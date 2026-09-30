"""
BASE ENGINE v2 - Tüm motorların ortak çalışma mantığı.

Akış:
  1) Güvenlik kapıları: kill-switch, motor duraklatma, haber filtresi, AI modeli hazır mı
  2) Her parite için SADECE KAPANMIŞ mumlarla göstergeleri hesapla
  3) Kurulum (setup) tetiklendi mi? (trend_pullback / range_reversion / liquidity_sweep)
  4) AI filtresi: "bu sinyal komisyon sonrası kâr eder mi?" olasılığı >= eşik mi?
  5) Risk: %1 kasa riski, portföy/aynı yön tavanları, min. emir büyüklüğü, marj
  6) Market emir -> SL/TP gerçek dolum fiyatına göre -> DB + Telegram
"""
import json
import logging
import time
from datetime import datetime

from backend import config
from backend.ai import model as ai_model
from backend.db import repository
from backend.db.models import TradeStatus
from backend.exchange import symbol_scanner
from backend.exchange.binance_client import UnprotectedPositionError
from backend.risk import correlation, kill_switch, news_filter, position_sizing
from backend.strategies import features as feat
from backend.strategies import setups as st
from backend.strategies.indicators import drop_unclosed
from backend.telegram import notifier

logger = logging.getLogger("base_engine")

# Stop mesafesi fiyatın bu yüzdesini aşarsa işlem açılmaz (config.MAX_STOP_PCT ile değiştirilebilir)
MAX_STOP_PCT_DEFAULT = {"scalp": 0.02, "day": 0.035, "swing": 0.07}

_last_processed_bar = {}   # (engine, symbol) -> son değerlendirilen mum ts (aynı mumda 2 kez girme)
_last_ready_msg = {}


def evaluate_symbol(engine_name: str, symbol: str, client):
    """Bir parite için sinyal değerlendirir. Dönüş: None veya sinyal sözlüğü."""
    tfs = config.ENGINE_TIMEFRAMES[engine_name]
    entry = drop_unclosed(client.fetch_ohlcv(symbol, tfs["entry"], limit=config.ENTRY_CANDLE_LIMIT), tfs["entry"])
    confirm = drop_unclosed(client.fetch_ohlcv(symbol, tfs["confirm"], limit=config.CONFIRM_CANDLE_LIMIT), tfs["confirm"])
    if len(entry) < 300 or len(confirm) < 60:
        return None

    key = (engine_name, symbol)
    last_ts = entry[-1][0]
    if _last_processed_bar.get(key) == last_ts:
        return None
    _last_processed_bar[key] = last_ts

    frame = feat.build_frame(entry, confirm, tfs["entry"], tfs["confirm"])
    sides, names = st.detect(frame, config.ENGINE_SETUPS[engine_name])
    side_val, setup = int(sides[-1]), names[-1]
    if side_val == 0:
        return None

    row = frame.iloc[-1]
    vector = feat.model_vector(row, side_val, setup)
    allowed, prob = ai_model.evaluate(engine_name, vector)
    prob_txt = f"{prob:.2f}" if prob is not None else "-"
    if not allowed:
        logger.info(f"[{engine_name}] {symbol}: {setup} {'LONG' if side_val > 0 else 'SHORT'} sinyali "
                    f"AI tarafından reddedildi (olasılık {prob_txt}).")
        return None

    return {
        "side": "buy" if side_val > 0 else "sell",
        "setup": setup,
        "probability": prob,
        "atr": float(row["atr"]),
        "ref_price": float(row["close"]),
        "vector": vector,
    }


def run_cycle(engine_name: str, client):
    if not config.ENGINE_ENABLED.get(engine_name):
        return

    kill_switch.reactivate_engine_if_pause_expired(engine_name)
    if kill_switch.is_global_kill_switch_active():
        logger.warning(f"[{engine_name}] 🛑 GÜNLÜK ZARAR LİMİTİ - bugün yeni işlem açılmayacak.")
        return
    if not repository.is_engine_active(engine_name):
        logger.info(f"[{engine_name}] motor duraklatılmış (art arda kayıp), döngü atlandı.")
        return
    if news_filter.is_news_blackout_active():
        logger.info(f"[{engine_name}] 📰 haber blackout aktif.")
        return

    ready, msg = ai_model.engine_ready(engine_name)
    if _last_ready_msg.get(engine_name) != msg:
        (logger.info if ready else logger.warning)(f"[{engine_name}] AI durumu: {msg}")
        _last_ready_msg[engine_name] = msg
    if not ready:
        return

    open_count = repository.count_open_positions(engine_name)
    max_positions = config.MAX_CONCURRENT_POSITIONS[engine_name]

    for symbol in symbol_scanner.get_active_symbols(client):
        if open_count >= max_positions:
            break
        if not correlation.total_open_risk_ok(repository.get_virtual_balance()):
            logger.info(f"[{engine_name}] toplam açık risk tavanında, yeni işlem yok.")
            break
        if repository.has_open_position_any_engine(symbol) or repository.is_symbol_in_cooldown(symbol):
            continue
        time.sleep(config.API_REQUEST_SPACING_SECONDS)
        try:
            signal = evaluate_symbol(engine_name, symbol, client)
            if signal and _open_trade(engine_name, symbol, signal, client):
                open_count += 1
        except UnprotectedPositionError:
            raise
        except Exception as e:
            logger.error(f"[{engine_name}] {symbol} işlenirken hata: {e}")


def _open_trade(engine_name: str, symbol: str, signal: dict, client) -> bool:
    side = signal["side"]
    try:
        price = float(client.fetch_ticker(symbol)["last"])
    except Exception:
        price = signal["ref_price"]

    # ATR gerçek piyasa verisinden; testnet fiyatı farklı olsa bile yüzde olarak uygula
    atr_pct = signal["atr"] / signal["ref_price"]
    dist = position_sizing.stop_distance(engine_name, price, atr_pct * price)
    tp_dist = dist * config.EXIT_PARAMS[engine_name]["tp_r"]

    # Stop çok genişse (pompalanan / aşırı oynak coin) işlem açma.
    # Örn. MOVR: scalp işleminde stop %4.4 çıkmıştı; gürültüyle vurulma ihtimali çok yüksek.
    max_stop_pct = getattr(config, "MAX_STOP_PCT", MAX_STOP_PCT_DEFAULT)[engine_name]
    if dist / price > max_stop_pct:
        logger.info(f"[{engine_name}] {symbol}: stop %{dist / price * 100:.1f} çok geniş "
                    f"(limit %{max_stop_pct * 100:.1f}), aşırı oynak coin - atlandı.")
        return False

    balance = repository.get_virtual_balance()
    sizing = position_sizing.calculate_position(engine_name, price, dist, balance)

    if sizing["notional_usd"] < client.min_notional(symbol):
        logger.info(f"[{engine_name}] {symbol}: pozisyon ({sizing['notional_usd']}$) borsa minimumunun altında.")
        return False
    if not correlation.check_correlation_limit(symbol, side, sizing["risk_usd"], balance):
        return False
    exchange_balance = client.get_usdt_balance()
    if not position_sizing.check_margin_sufficient(sizing["required_margin_usd"], exchange_balance):
        logger.warning(f"[{engine_name}] {symbol}: yetersiz marj ({sizing['required_margin_usd']}$ / {exchange_balance}$).")
        return False

    client.set_leverage(symbol, sizing["leverage"])
    try:
        res = client.open_position(symbol, side, sizing["amount"], stop_distance=dist, tp_distance=tp_dist)
    except UnprotectedPositionError as e:
        logger.critical(str(e))
        notifier.send_alert(str(e))
        raise

    prob = signal["probability"]
    trade = repository.save_trade(
        engine=engine_name,
        symbol=symbol,
        side=side,
        leverage=sizing["leverage"],
        entry_price=res["fill_price"],
        amount=res["amount"],
        risk_usd=round(res["amount"] * dist, 4),
        stop_loss=res["stop_loss"],
        take_profit_1=res["take_profit"],
        take_profit_2=res["take_profit"],   # tek hedef: küçük ve net kazanç
        status=TradeStatus.OPEN,
        confluence_score=int(round((prob or 0) * 100)),
        strategies_used=signal["setup"],
        setup=signal["setup"],
        ai_probability=prob,
        features_json=json.dumps(signal["vector"]),
        sl_algo_id=res.get("sl_algo_id"),
        tp_algo_id=res.get("tp_algo_id"),
        opened_at=datetime.utcnow(),
    )
    logger.info(f"[{engine_name}] ✅ {symbol} {side.upper()} @ {res['fill_price']} | {signal['setup']} "
                f"| AI: {prob if prob is None else round(prob, 2)} | {sizing['leverage']}x "
                f"| SL {res['stop_loss']:.6g} TP {res['take_profit']:.6g} | risk {trade.risk_usd:.2f}$")
    notifier.send_trade_opened(trade)
    return True
