"""
POZİSYON İZLEME v2

Eski sürümdeki "kademeli kâr koruma" kaldırıldı. O sistem:
  - fiyat 0.5R'ye (1 dakikalıkta tek bir fitil) değince stopu başa-başa çekiyor,
  - en iyi seviyeden %40 geri çekilince işlemi kapatıyordu.
Sonuç: kazananlar ~0.3-0.6R'de kesiliyor, kaybedenler tam -1R oluyordu.
Bu yapı %70+ kazanma oranı gerektirir; matematiksel olarak kaybettiriyordu.

Yeni sürüm (backtest ile BİREBİR aynı kurallar):
  1) KAPANIŞ TESPİTİ: TP/SL tetiklendiyse gerçek PnL'i (komisyon dahil) borsadan al
  2) ZAMAN STOPU: max_hold_bars dolan işlem piyasa fiyatından kapatılır
  3) OPSİYONEL BREAKEVEN: config'de breakeven_r ayarlıysa (varsayılan kapalı)
  4) RECONNECT SENKRONİZASYONU
"""
import logging
import time
from datetime import datetime, timezone

from backend import config
from backend.db import repository
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus
from backend.risk import kill_switch
from backend.risk.costs import estimate_fees_usd, estimate_net_pnl
from backend.strategies.indicators import TF_MS, drop_unclosed
from backend.telegram import notifier

logger = logging.getLogger("position_monitor")


def _wanted_exchange_side(trade_side: str) -> str:
    return "long" if trade_side == "buy" else "short"


def _find_exchange_position(trade: Trade, exchange_positions: list):
    wanted = _wanted_exchange_side(trade.side)
    for p in exchange_positions:
        if p.get("symbol") == trade.symbol and p.get("side") == wanted and float(p.get("contracts", 0) or 0) != 0:
            return p
    return None


def _opened_ms(trade: Trade) -> int:
    return int(trade.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _algo_outcome(trade: Trade, client):
    for algo_id, name in ((trade.tp_algo_id, "tp"), (trade.sl_algo_id, "sl")):
        if not algo_id:
            continue
        try:
            order = client.get_algo_order(algo_id)
            if order.get("algoStatus") == "FINISHED":
                price = float(order.get("actualPrice") or 0)
                return name, (price if price > 0 else None)
        except Exception as e:
            logger.warning(f"{name.upper()} algo emri sorgulanamadı ({algo_id}): {e}")
    return None, None


def _finalize(trade: Trade, exit_price: float, status: TradeStatus, client, event_label: str = None):
    """PnL'i hesapla (mümkünse borsadan gerçek), DB + kasa + kill-switch + Telegram."""
    pnl_usd, fees = None, None
    try:
        time.sleep(1.0)  # borsa işlem geçmişinin güncellenmesi için kısa bekleme
        real = client.fetch_realized_pnl(trade.symbol, _opened_ms(trade) - 5_000)
        if real is not None:
            pnl_usd, fees, last_px = real
            exit_price = exit_price or last_px
    except Exception as e:
        logger.warning(f"[{trade.engine}] {trade.symbol}: gerçek PnL alınamadı, tahmin kullanılacak: {e}")

    if exit_price is None:
        try:
            exit_price = float(client.fetch_ticker(trade.symbol)["last"])
        except Exception:
            exit_price = trade.entry_price
    if pnl_usd is None:
        pnl_usd = estimate_net_pnl(trade.side, trade.entry_price, exit_price, trade.amount)
        fees = estimate_fees_usd(trade.entry_price, exit_price, trade.amount)

    if not repository.close_trade(trade.id, exit_price, pnl_usd, status, fees_usd=fees):
        return
    new_balance = repository.update_virtual_balance(pnl_usd)
    kill_switch.record_realized_pnl(pnl_usd, new_balance - pnl_usd)
    kill_switch.record_engine_trade_result(trade.engine, is_win=(pnl_usd > 0))

    logger.info(f"[{trade.engine}] 📌 {trade.symbol} kapandı: {status.value} | net PnL {pnl_usd:.2f}$ "
                f"(komisyon {fees or 0:.2f}$) | kasa {new_balance:.2f}$")
    trade.pnl_usd = pnl_usd
    notifier.send_trade_update(trade, event_label or status.value)


def _handle_trade_closed(trade: Trade, client):
    outcome, price = _algo_outcome(trade, client)
    try:
        client.cancel_all_algo_orders(trade.symbol)  # arta kalan TP/SL'i temizle
    except Exception as e:
        logger.warning(f"[{trade.engine}] {trade.symbol}: arta kalan emirler iptal edilemedi: {e}")

    if outcome == "tp":
        status = TradeStatus.CLOSED_TP1
        price = price or trade.take_profit_1   # borsa gerçekleşme fiyatı vermediyse hedef fiyat
    elif outcome == "sl":
        status = TradeStatus.CLOSED_BE if trade.moved_to_breakeven else TradeStatus.CLOSED_SL
        price = price or trade.stop_loss
    else:
        status = TradeStatus.CLOSED_MANUAL
    _finalize(trade, price, status, client)


def _close_now(trade: Trade, client, label: str):
    try:
        client.cancel_all_algo_orders(trade.symbol)
        client.close_position(trade.symbol, trade.side, trade.amount)
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: kapatma başarısız: {e}")
        return
    _finalize(trade, None, TradeStatus.CLOSED_MANUAL, client, event_label=label)


def _check_time_stop(trade: Trade, client) -> bool:
    tf = config.ENGINE_TIMEFRAMES[trade.engine]["entry"]
    max_bars = config.EXIT_PARAMS[trade.engine]["max_hold_bars"]
    elapsed_bars = (time.time() * 1000 - _opened_ms(trade)) / TF_MS[tf]
    if elapsed_bars >= max_bars:
        logger.info(f"[{trade.engine}] ⏱️ {trade.symbol}: {max_bars} mum doldu, zaman stopu.")
        _close_now(trade, client, f"⏱️ Zaman stopu ({max_bars} mum)")
        return True
    return False


def _check_breakeven(trade: Trade, client):
    be_r = config.EXIT_PARAMS[trade.engine].get("breakeven_r")
    if not be_r or trade.moved_to_breakeven:
        return
    tf = config.ENGINE_TIMEFRAMES[trade.engine]["entry"]
    candles = drop_unclosed(client.fetch_ohlcv(trade.symbol, tf, limit=100), tf)
    candles = [c for c in candles if c[0] >= _opened_ms(trade)]
    if not candles:
        return
    dist = abs(trade.entry_price - trade.stop_loss)
    if trade.side == "buy":
        reached = max(c[2] for c in candles) >= trade.entry_price + be_r * dist
        new_stop = trade.entry_price * (1 + 2 * config.COST_PER_SIDE)
    else:
        reached = min(c[3] for c in candles) <= trade.entry_price - be_r * dist
        new_stop = trade.entry_price * (1 - 2 * config.COST_PER_SIDE)
    if not reached:
        return
    try:
        new_id = client.update_stop_loss(trade.symbol, trade.side, trade.amount, new_stop, old_algo_id=trade.sl_algo_id)
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: breakeven stop konulamadı: {e}")
        return
    session = get_session()
    try:
        t = session.query(Trade).filter(Trade.id == trade.id).first()
        if t:
            t.stop_loss, t.sl_algo_id, t.moved_to_breakeven = new_stop, new_id, True
            session.commit()
    finally:
        session.close()
    logger.info(f"[{trade.engine}] 🔒 {trade.symbol}: stop başa-başa (+maliyet) çekildi.")


def run_monitor_cycle(client):
    open_trades = repository.get_open_trades()
    if not open_trades:
        return
    try:
        exchange_positions = client.fetch_open_positions()
    except Exception as e:
        logger.error(f"Açık pozisyonlar alınamadı: {e}")
        return

    for trade in open_trades:
        time.sleep(config.API_REQUEST_SPACING_SECONDS)
        try:
            if _find_exchange_position(trade, exchange_positions) is None:
                _handle_trade_closed(trade, client)
                continue
            if _check_time_stop(trade, client):
                continue
            _check_breakeven(trade, client)
        except Exception as e:
            logger.error(f"[{trade.engine}] {trade.symbol} izlenirken hata: {e}")


def reconcile_positions_on_startup(client):
    logger.info("=== RECONNECT SENKRONİZASYONU ===")
    try:
        exchange_positions = client.fetch_open_positions()
    except Exception as e:
        logger.error(f"Senkronizasyon için pozisyonlar alınamadı: {e}")
        return
    db_trades = repository.get_open_trades()
    for trade in db_trades:
        if _find_exchange_position(trade, exchange_positions) is None:
            logger.warning(f"[{trade.engine}] {trade.symbol}: DB'de açık ama borsada yok -> kapatılmış sayılıyor.")
            _handle_trade_closed(trade, client)
    matched = {(t.symbol, _wanted_exchange_side(t.side)) for t in db_trades}
    for p in exchange_positions:
        if (p.get("symbol"), p.get("side")) not in matched:
            logger.warning(f"⚠️ Borsada DB kaydı olmayan pozisyon: {p.get('symbol')} {p.get('side')} - elle kontrol et.")
