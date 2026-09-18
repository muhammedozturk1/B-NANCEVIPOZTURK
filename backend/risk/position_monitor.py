"""
POZİSYON İZLEME Modülü.

Bu modül, açık pozisyonları periyodik olarak (varsayılan 30sn'de bir) kontrol eder:

  1) BREAKEVEN: TP1'e ulaşıldıysa stop-loss girişe (breakeven) çekilir.
  2) MOMENTUM KAYBI: Fiyat hedefe doğru ilerlerken belirgin şekilde geri
     dönmeye başladıysa (TP1'e ulaşmadan), pozisyon erken kapatılır.
  3) KAPANIŞ TESPİTİ: Borsada TP/SL emriyle kapanmış ama veritabanında hâlâ
     "açık" görünen işlemleri tespit edip DB'yi günceller, kill-switch ve
     motor bazlı kayıp sayaçlarını besler.
  4) RECONNECT SENKRONİZASYONU: Bot yeniden başladığında, borsadaki gerçek
     açık pozisyonlarla veritabanını karşılaştırıp "yetim" kayıt bırakmaz.

NOT (bilinçli basitleştirme): Kapanış fiyatı, borsanın emir geçmişi yerine
anlık ticker fiyatından okunur; bu, gerçek kapanış fiyatından çok küçük bir
sapma gösterebilir ama işlem takibi için yeterlidir.
"""
import logging
from backend import config
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus
from backend.db import repository
from backend.risk import kill_switch
from backend.telegram import notifier

logger = logging.getLogger("position_monitor")


def _wanted_exchange_side(trade_side: str) -> str:
    return "long" if trade_side == "buy" else "short"


def _find_exchange_position(trade: Trade, exchange_positions: list):
    wanted_side = _wanted_exchange_side(trade.side)
    for p in exchange_positions:
        if p.get("symbol") == trade.symbol and p.get("side") == wanted_side \
                and float(p.get("contracts", 0) or 0) != 0:
            return p
    return None


def _estimate_pnl(trade: Trade, exit_price: float) -> float:
    direction = 1 if trade.side == "buy" else -1
    return (exit_price - trade.entry_price) * trade.amount * direction


def _guess_close_status(trade: Trade, exit_price: float) -> TradeStatus:
    """Kapanış fiyatının hangi seviyeye (SL/TP1/TP2/BE) en yakın olduğuna bakarak tahmin eder."""
    candidates = {
        TradeStatus.CLOSED_SL: trade.stop_loss,
        TradeStatus.CLOSED_TP1: trade.take_profit_1,
        TradeStatus.CLOSED_TP2: trade.take_profit_2,
    }
    if trade.moved_to_breakeven:
        candidates[TradeStatus.CLOSED_BE] = trade.entry_price

    return min(candidates, key=lambda status: abs(candidates[status] - exit_price))


def _handle_trade_closed(trade: Trade, client):
    try:
        ticker = client.fetch_ticker(trade.symbol)
        exit_price = ticker["last"]
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: kapanış fiyatı alınamadı, entry fiyat kullanılacak: {e}")
        exit_price = trade.entry_price

    pnl_usd = _estimate_pnl(trade, exit_price)
    status = _guess_close_status(trade, exit_price)

    repository.close_trade(trade.id, exit_price, pnl_usd, status)

    try:
        current_balance = client.get_usdt_balance()
    except Exception:
        current_balance = trade.entry_price  # son çare, kill-switch hesaplamasını bozmasın diye kabaca

    kill_switch.record_realized_pnl(pnl_usd, current_balance)
    kill_switch.record_engine_trade_result(trade.engine, is_win=(pnl_usd > 0))

    logger.info(f"[{trade.engine}] 📌 İşlem kapandı: {trade.symbol} {status.value} | PnL: {pnl_usd:.2f}$")
    notifier.send_trade_update(trade, status.value)


def _check_breakeven(trade: Trade, current_price: float, client):
    if trade.moved_to_breakeven:
        return

    reached_tp1 = (current_price >= trade.take_profit_1) if trade.side == "buy" \
        else (current_price <= trade.take_profit_1)

    if not reached_tp1:
        return

    try:
        client.update_stop_loss(trade.symbol, trade.side, trade.amount, trade.entry_price)
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: breakeven'a çekilemedi: {e}")
        return

    session = get_session()
    try:
        db_trade = session.query(Trade).filter(Trade.id == trade.id).first()
        if db_trade:
            db_trade.moved_to_breakeven = True
            session.commit()
    finally:
        session.close()

    logger.info(f"[{trade.engine}] 🔒 {trade.symbol}: TP1'e ulaşıldı, stop girişe (breakeven) çekildi.")
    notifier.send_trade_update(trade, "BE (girişe çekildi)")


def _check_momentum_reversal(trade: Trade, candles: list, client):
    """TP1'e henüz ulaşmadan, fiyat lehimize gidip belirgin geri dönerse pozisyonu kapat."""
    if trade.moved_to_breakeven:
        return  # TP1 zaten geçildi, bu artık momentum-kaybı değil normal breakeven süreci

    current_price = candles[-1][4]

    if trade.side == "buy":
        best_price = max(c[2] for c in candles)  # lookback içindeki en yüksek fiyat
        favorable_move = best_price - trade.entry_price
        if favorable_move <= 0:
            return  # henüz kârda değiliz, momentum kaybı değerlendirmesi erken
        retrace = best_price - current_price
    else:
        best_price = min(c[3] for c in candles)
        favorable_move = trade.entry_price - best_price
        if favorable_move <= 0:
            return
        retrace = current_price - best_price

    retrace_ratio = retrace / favorable_move

    if retrace_ratio >= config.MOMENTUM_REVERSAL_RETRACE_PERCENT:
        try:
            client.cancel_open_stop_orders(trade.symbol)
            client.close_position(trade.symbol, trade.side, trade.amount)
        except Exception as e:
            logger.error(f"[{trade.engine}] {trade.symbol}: momentum kaybı kapatması başarısız: {e}")
            return

        pnl_usd = _estimate_pnl(trade, current_price)
        repository.close_trade(trade.id, current_price, pnl_usd, TradeStatus.CLOSED_MANUAL)

        try:
            current_balance = client.get_usdt_balance()
        except Exception:
            current_balance = trade.entry_price
        kill_switch.record_realized_pnl(pnl_usd, current_balance)
        kill_switch.record_engine_trade_result(trade.engine, is_win=(pnl_usd > 0))

        logger.info(f"[{trade.engine}] ⚠️ {trade.symbol}: momentum kaybı nedeniyle erken kapatıldı "
                    f"(geri çekilme: %{retrace_ratio*100:.0f}) | PnL: {pnl_usd:.2f}$")
        notifier.send_trade_update(trade, "erken kapama (momentum kaybı)")


def run_monitor_cycle(client):
    """Ana döngüden periyodik olarak çağrılır."""
    open_trades = repository.get_open_trades()
    if not open_trades:
        return

    try:
        exchange_positions = client.fetch_open_positions()
    except Exception as e:
        logger.error(f"Açık pozisyonlar borsadan alınamadı: {e}")
        return

    for trade in open_trades:
        try:
            position = _find_exchange_position(trade, exchange_positions)

            if position is None:
                _handle_trade_closed(trade, client)
                continue

            timeframe = config.ENGINE_TIMEFRAMES[trade.engine]["entry"]
            candles = client.fetch_ohlcv(trade.symbol, timeframe, limit=config.MOMENTUM_LOOKBACK_CANDLES)
            current_price = candles[-1][4]

            _check_breakeven(trade, current_price, client)
            _check_momentum_reversal(trade, candles, client)

        except Exception as e:
            logger.error(f"[{trade.engine}] {trade.symbol} izlenirken hata: {e}")
            continue


def reconcile_positions_on_startup(client):
    """
    Bot yeniden başladığında (reconnect sonrası) çağrılır:
    - DB'de açık görünen ama borsada kapanmış işlemleri kapatır.
    - Borsada açık olup DB'de hiç kaydı olmayan pozisyonlar varsa (örn. manuel
      açılmış veya bot çökmeden hemen önce kaydedilememiş) uyarı loglar.
    """
    logger.info("=== RECONNECT SENKRONİZASYONU BAŞLIYOR ===")
    try:
        exchange_positions = client.fetch_open_positions()
    except Exception as e:
        logger.error(f"Senkronizasyon için borsa pozisyonları alınamadı: {e}")
        return

    db_trades = repository.get_open_trades()

    for trade in db_trades:
        if _find_exchange_position(trade, exchange_positions) is None:
            logger.warning(f"[{trade.engine}] {trade.symbol}: DB'de açık ama borsada yok, kapanmış kabul ediliyor.")
            _handle_trade_closed(trade, client)

    matched_symbols = {(t.symbol, _wanted_exchange_side(t.side)) for t in db_trades}
    for position in exchange_positions:
        key = (position.get("symbol"), position.get("side"))
        if key not in matched_symbols:
            logger.warning(f"⚠️ DİKKAT: Borsada DB'de kaydı olmayan açık pozisyon bulundu: "
                            f"{position.get('symbol')} {position.get('side')} - manuel kontrol gerekebilir.")

    logger.info("=== RECONNECT SENKRONİZASYONU TAMAMLANDI ===")
