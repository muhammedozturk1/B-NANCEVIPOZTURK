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
import time
from datetime import timezone
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


def _determine_close_outcome(trade: Trade, client):
    """
    Pozisyonun GERÇEKTEN hangi emirle (TP mi SL mi) ve hangi fiyattan kapandığını
    Binance'in algo emir kayıtlarından sorgular. Tahmine dayalı eski yöntemin
    (en yakın fiyat seviyesini bulma) yanlış sınıflandırma yaptığı durumları çözer.

    Dönüş: (outcome, actual_price) -> outcome: "tp" | "sl" | None (belirsizse)
    """
    tp_filled_price = None
    sl_filled_price = None

    if trade.tp_algo_id:
        try:
            tp_order = client.get_algo_order(trade.tp_algo_id)
            if tp_order.get("algoStatus") == "FINISHED":
                price = float(tp_order.get("actualPrice") or 0)
                tp_filled_price = price if price > 0 else trade.take_profit_1
        except Exception as e:
            logger.warning(f"TP algo emri sorgulanamadı ({trade.tp_algo_id}): {e}")

    if trade.sl_algo_id:
        try:
            sl_order = client.get_algo_order(trade.sl_algo_id)
            if sl_order.get("algoStatus") == "FINISHED":
                price = float(sl_order.get("actualPrice") or 0)
                sl_filled_price = price if price > 0 else trade.stop_loss
        except Exception as e:
            logger.warning(f"SL algo emri sorgulanamadı ({trade.sl_algo_id}): {e}")

    if tp_filled_price is not None:
        return "tp", tp_filled_price
    if sl_filled_price is not None:
        return "sl", sl_filled_price
    return None, None


def _handle_trade_closed(trade: Trade, client):
    outcome, actual_price = _determine_close_outcome(trade, client)

    if outcome == "tp":
        exit_price = actual_price
        status = TradeStatus.CLOSED_TP1  # şu an tek TP emri var, her zaman TP1 seviyesinde
    elif outcome == "sl":
        exit_price = actual_price
        # Breakeven sonrası SL, girişe çekilmiş SL'dir -> bu gerçekte "breakeven'da kapandı" demektir
        status = TradeStatus.CLOSED_BE if trade.moved_to_breakeven else TradeStatus.CLOSED_SL
    else:
        # Gerçek emir durumu belirlenemedi (örn. eski algo_id'siz kayıt) -> eski tahmine dayalı yönteme düş
        try:
            ticker = client.fetch_ticker(trade.symbol)
            exit_price = ticker["last"]
        except Exception as e:
            logger.error(f"[{trade.engine}] {trade.symbol}: kapanış fiyatı alınamadı, entry fiyat kullanılacak: {e}")
            exit_price = trade.entry_price
        status = _guess_close_status(trade, exit_price)

    pnl_usd = _estimate_pnl(trade, exit_price)

    repository.close_trade(trade.id, exit_price, pnl_usd, status)
    new_virtual_balance = repository.update_virtual_balance(pnl_usd)

    kill_switch.record_realized_pnl(pnl_usd, new_virtual_balance - pnl_usd)
    kill_switch.record_engine_trade_result(trade.engine, is_win=(pnl_usd > 0))

    logger.info(f"[{trade.engine}] 📌 İşlem kapandı: {trade.symbol} {status.value} | PnL: {pnl_usd:.2f}$ "
                f"| Sanal kasa: {new_virtual_balance:.2f}$")
    notifier.send_trade_update(trade, status.value)


def _check_breakeven(trade: Trade, current_price: float, client):
    if trade.moved_to_breakeven:
        return

    reached_tp1 = (current_price >= trade.take_profit_1) if trade.side == "buy" \
        else (current_price <= trade.take_profit_1)

    if not reached_tp1:
        return

    try:
        new_sl_algo_id = client.update_stop_loss(trade.symbol, trade.side, trade.amount, trade.entry_price)
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: breakeven'a çekilemedi: {e}")
        return

    session = get_session()
    try:
        db_trade = session.query(Trade).filter(Trade.id == trade.id).first()
        if db_trade:
            db_trade.moved_to_breakeven = True
            db_trade.sl_algo_id = new_sl_algo_id  # ÖNEMLİ: eski SL iptal oldu, yeni ID'yi takip etmeliyiz
            session.commit()
    finally:
        session.close()

    logger.info(f"[{trade.engine}] 🔒 {trade.symbol}: TP1'e ulaşıldı, stop girişe (breakeven) çekildi.")
    notifier.send_trade_update(trade, "BE (girişe çekildi)")


def _check_momentum_reversal(trade: Trade, candles: list, client):
    """TP1'e henüz ulaşmadan, fiyat lehimize gidip belirgin geri dönerse pozisyonu kapat.

    KRİTİK DÜZELTME: 'candles' parametresi son N mumu (işlem açılmadan ÖNCEKİ mumlar
    dahil) içerir. Eskiden 'en iyi fiyat' hesabı bu ÖNCEKİ mumları da tarıyordu -
    işlem yeni açılmış olsa bile, açılıştan önceki bir fiyat tepesini "bu işlem
    kâr etmişti, şimdi geri çekiliyor" diye yanlış yorumlayıp anında (birkaç saniye
    içinde) pozisyonu kapatıyordu. Artık SADECE trade.opened_at'ten SONRAKİ mumlar
    değerlendiriliyor."""
    if trade.moved_to_breakeven:
        return

    opened_at_ms = int(trade.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    relevant_candles = [c for c in candles if c[0] >= opened_at_ms]

    if len(relevant_candles) < 2:
        return  # işlem çok yeni, henüz değerlendirilecek yeterli veri yok

    current_price = relevant_candles[-1][4]

    if trade.side == "buy":
        best_price = max(c[2] for c in relevant_candles)  # işlem açıldıktan SONRAKİ en yüksek fiyat
        favorable_move = best_price - trade.entry_price
        if favorable_move <= 0:
            return  # henüz kârda değiliz, momentum kaybı değerlendirmesi erken
        retrace = best_price - current_price
    else:
        best_price = min(c[3] for c in relevant_candles)
        favorable_move = trade.entry_price - best_price
        if favorable_move <= 0:
            return
        retrace = current_price - best_price

    retrace_ratio = retrace / favorable_move

    if retrace_ratio >= config.MOMENTUM_REVERSAL_RETRACE_PERCENT:
        try:
            client.cancel_all_algo_orders(trade.symbol)
            client.close_position(trade.symbol, trade.side, trade.amount)
        except Exception as e:
            logger.error(f"[{trade.engine}] {trade.symbol}: momentum kaybı kapatması başarısız: {e}")
            return

        pnl_usd = _estimate_pnl(trade, current_price)
        repository.close_trade(trade.id, current_price, pnl_usd, TradeStatus.CLOSED_MANUAL)
        new_virtual_balance = repository.update_virtual_balance(pnl_usd)

        kill_switch.record_realized_pnl(pnl_usd, new_virtual_balance - pnl_usd)
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
        time.sleep(config.API_REQUEST_SPACING_SECONDS)
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
