"""
BASE ENGINE - Tüm motorların (scalp/day/swing) ortak çalışma mantığı.

Akış:
  1) Motora atanmış teknikleri çalıştır, her biri long/short/neutral oy versin
  2) Oyları topla (confluence score), eşik geçildiyse yön belirle
  3) Üst zaman dilimi (confirm timeframe) trendine ters değilse devam et
  4) Kaldıraç + Stop-Loss + TP1/TP2 + pozisyon büyüklüğünü hesapla
  5) Marj yeterliyse pozisyonu aç, veritabanına kaydet, bildirim gönder
"""
import logging
from backend import config
from backend.db import repository
from backend.db.models import TradeStatus
from backend.risk import position_sizing, kill_switch, correlation, news_filter
from backend.strategies import ema, vwap, smc, liquidity, support_resistance, fear_greed
from backend.telegram import notifier

logger = logging.getLogger("base_engine")

STRATEGY_MODULES = {
    "ema": ema,
    "vwap": vwap,
    "smc": smc,
    "liquidity_zones": liquidity,
    "support_resistance": support_resistance,
    "fear_greed": fear_greed,
}


def _collapse_direction(direction: str):
    """'long_weak' -> ('long', 0.5) gibi normalize eder. Tam sinyal ağırlığı 1.0'dır."""
    if direction in ("long", "short"):
        return direction, 1.0
    if direction in ("long_weak", "short_weak"):
        return direction.replace("_weak", ""), 0.5
    return "neutral", 0.0


def evaluate_signals(engine_name: str, entry_candles: list) -> dict:
    """Motora atanmış tüm teknikleri çalıştırır ve confluence skorunu hesaplar."""
    votes = {"long": 0.0, "short": 0.0}
    details = {}

    for strat_name in config.ENGINE_STRATEGIES[engine_name]:
        module = STRATEGY_MODULES[strat_name]
        try:
            signal = module.get_signal() if strat_name == "fear_greed" else module.get_signal(entry_candles)
        except Exception as e:
            logger.error(f"[{engine_name}] {strat_name} stratejisi hata verdi: {e}")
            signal = {"direction": "neutral"}

        details[strat_name] = signal
        norm_direction, weight = _collapse_direction(signal.get("direction", "neutral"))
        if norm_direction in votes:
            votes[norm_direction] += weight

    threshold = config.CONFLUENCE_THRESHOLD[engine_name]

    if votes["long"] >= threshold and votes["long"] > votes["short"]:
        side, score = "buy", votes["long"]
    elif votes["short"] >= threshold and votes["short"] > votes["long"]:
        side, score = "sell", votes["short"]
    else:
        side, score = None, max(votes["long"], votes["short"])

    return {"side": side, "score": score, "votes": votes, "details": details}


def check_mtf_confirmation(side: str, confirm_candles: list) -> bool:
    """Üst zaman dilimi trendi sinyale ters mi diye kontrol eder. Ters ise False döner (işlem iptal)."""
    mtf_signal = ema.get_signal(confirm_candles)
    mtf_direction, _ = _collapse_direction(mtf_signal.get("direction", "neutral"))

    if mtf_direction == "neutral":
        return True  # üst TF net değilse engel olmayalım
    if side == "buy" and mtf_direction == "short":
        return False
    if side == "sell" and mtf_direction == "long":
        return False
    return True


def run_cycle(engine_name: str, client):
    """Bir motor için tek bir kontrol döngüsü: tüm sembolleri tarar, uygun olan(lar)da işlem açar."""

    kill_switch.reactivate_engine_if_pause_expired(engine_name)

    if kill_switch.is_global_kill_switch_active():
        logger.warning(f"[{engine_name}] 🛑 GENEL KILL-SWITCH AKTİF - bugün yeni işlem açılmayacak.")
        return

    if not repository.is_engine_active(engine_name):
        logger.info(f"[{engine_name}] motor duraklatılmış durumda, bu döngü atlanıyor.")
        return

    if news_filter.is_news_blackout_active():
        logger.info(f"[{engine_name}] 📰 haber blackout aktif, yeni işlem açılmayacak.")
        return

    open_count = repository.count_open_positions(engine_name)
    max_positions = config.MAX_CONCURRENT_POSITIONS[engine_name]

    for symbol in config.SYMBOLS:
        try:
            if open_count >= max_positions:
                logger.info(f"[{engine_name}] maksimum eşzamanlı pozisyon limitine ulaşıldı ({max_positions}).")
                break

            if repository.has_open_position(engine_name, symbol):
                continue  # bu sembolde zaten açık pozisyon var

            timeframes = config.ENGINE_TIMEFRAMES[engine_name]
            entry_candles = client.fetch_ohlcv(symbol, timeframes["entry"], limit=200)
            confirm_candles = client.fetch_ohlcv(symbol, timeframes["confirm"], limit=200)

            evaluation = evaluate_signals(engine_name, entry_candles)
            side = evaluation["side"]

            if side is None:
                logger.debug(f"[{engine_name}] {symbol}: yeterli confluence yok (skor: {evaluation['score']}).")
                continue

            if not check_mtf_confirmation(side, confirm_candles):
                logger.info(f"[{engine_name}] {symbol}: üst zaman dilimi sinyale ters, işlem atlandı.")
                continue

            _open_trade(engine_name, symbol, side, entry_candles, evaluation, client)
            open_count += 1

        except Exception as e:
            logger.error(f"[{engine_name}] {symbol} işlenirken hata oluştu: {e}")
            continue


def _open_trade(engine_name: str, symbol: str, side: str, entry_candles: list, evaluation: dict, client):
    leverage = position_sizing.choose_leverage(entry_candles)
    atr = position_sizing.calculate_atr(entry_candles)
    entry_price = entry_candles[-1][4]
    sl_multiplier = config.SL_ATR_MULTIPLIER[engine_name]

    stop_loss = entry_price - atr * sl_multiplier if side == "buy" else entry_price + atr * sl_multiplier
    tp_prices = position_sizing.calculate_tp_prices(engine_name, entry_price, stop_loss, side)

    # Risk hesaplamaları (pozisyon büyüklüğü, korelasyon) SANAL kasaya göre yapılır.
    # Gerçek borsa bakiyesi sadece "marj fiziken yeterli mi" kontrolü için kullanılır.
    virtual_balance = repository.get_virtual_balance()
    exchange_balance = client.get_usdt_balance()

    sizing = position_sizing.calculate_position_size(engine_name, entry_price, stop_loss, virtual_balance, leverage)

    if not position_sizing.check_margin_sufficient(sizing["required_margin_usd"], exchange_balance):
        logger.warning(f"[{engine_name}] {symbol}: yetersiz marj, işlem açılamadı. "
                        f"Gereken: {sizing['required_margin_usd']}$, Borsa bakiyesi: {exchange_balance}$")
        return

    if not correlation.check_correlation_limit(symbol, side, sizing["risk_usd"], virtual_balance):
        logger.info(f"[{engine_name}] {symbol}: korelasyon limiti nedeniyle işlem açılmadı.")
        return

    client.set_leverage(symbol, leverage)
    client.open_position(symbol, side, sizing["amount"],
                          stop_loss=stop_loss, take_profit=tp_prices["take_profit_1"])

    trade = repository.save_trade(
        engine=engine_name,
        symbol=symbol,
        side=side,
        leverage=leverage,
        entry_price=entry_price,
        amount=sizing["amount"],
        risk_usd=sizing["risk_usd"],
        stop_loss=stop_loss,
        take_profit_1=tp_prices["take_profit_1"],
        take_profit_2=tp_prices["take_profit_2"],
        status=TradeStatus.OPEN,
        confluence_score=int(evaluation["score"]),
        strategies_used=",".join(config.ENGINE_STRATEGIES[engine_name]),
    )

    logger.info(f"[{engine_name}] ✅ YENİ İŞLEM: {symbol} {side.upper()} @ {entry_price} "
                f"| Kaldıraç: {leverage}x | SL: {stop_loss:.4f} | TP1: {tp_prices['take_profit_1']:.4f} "
                f"| TP2: {tp_prices['take_profit_2']:.4f} | Risk: {sizing['risk_usd']}$")

    notifier.send_trade_opened(trade)
