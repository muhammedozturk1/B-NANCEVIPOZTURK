def _update_protective_stop(trade: Trade, new_stop: float, client) -> bool:
    """
    Stop'u yeni bir seviyeye taşır (BE / trailing). Sadece DAHA İYİ bir seviyeye
    taşır (long'da daha yüksek, short'ta daha düşük). Başarılıysa True döner ve
    DB'yi günceller.
    """
    # Yön kontrolü: yeni stop gerçekten daha koruyucu mu?
    is_better = (
        (trade.side == "buy" and new_stop > trade.stop_loss) or
        (trade.side == "sell" and new_stop < trade.stop_loss)
    )
    if not is_better:
        return False

    try:
        new_sl_algo_id = client.update_stop_loss(
            trade.symbol, trade.side, trade.amount, new_stop
        )
    except Exception as e:
        logger.error(f"[{trade.engine}] {trade.symbol}: stop güncellenemedi: {e}")
        return False

    session = get_session()
    try:
        db_trade = session.query(Trade).filter(Trade.id == trade.id).first()
        if db_trade:
            old_stop = db_trade.stop_loss
            db_trade.stop_loss = new_stop
            db_trade.sl_algo_id = new_sl_algo_id
            # Breakeven'a çekildiyse işaretle (kapanışta BE olarak sınıflandırmak için)
            if trade.side == "buy":
                if new_stop >= trade.entry_price:
                    db_trade.moved_to_breakeven = True
            else:
                if new_stop <= trade.entry_price:
                    db_trade.moved_to_breakeven = True
            session.commit()
            logger.info(
                f"[{trade.engine}] 🔒 {trade.symbol}: stop güncellendi "
                f"{_fmt_p(old_stop)} -> {_fmt_p(new_stop)}"
            )
    finally:
        session.close()
    return True


def _fmt_p(p: float) -> str:
    if p >= 1000: return f"{p:.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"


def _check_profit_protection(trade: Trade, candles: list, client):
    """
    KADEMELİ KÂR KORUMA SİSTEMİ

    Üç katmanlı koruma:
      1) STOP İYİLEŞTİRME: Fiyat stop mesafesinin belirli katlarına ulaştıkça
         stop'u kademeli olarak yukarı (long) / aşağı (short) taşı.
         - %50 kâr -> stop = giriş (breakeven)
         - %75 kâr -> stop = giriş + %25 stop mesafesi
         - %100 kâr -> stop = giriş + %50 (TP1 seviyesinde zaten kâr realize olur)
         - %150 -> %75, %200 -> tümü (trailing)

      2) SERT KAPATMA (kâr erimesi): Fiyat en iyi seviyeden %40'tan fazla geri
         çekildiyse ve en az %50 kâra ulaşmışsak, pozisyonu tamamen kapat.

      3) TP1 sonrası: take-profit MARKET olduğu için TP1 fiyatına değildiğinde
         işlem otomatik kapanır; bu fonksiyon sadece TP1'e ulaşmadan önceki
         dalgalanmaları korur.
    """
    min_ratio = config.MOMENTUM_MIN_FAVORABLE_FRACTION.get(trade.engine)
    if min_ratio is None:
        return

    opened_at_ms = int(trade.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    relevant_candles = [c for c in candles if c[0] >= opened_at_ms]

    if len(relevant_candles) < 2:
        return

    current_price = relevant_candles[-1][4]
    stop_distance = abs(trade.entry_price - trade.stop_loss)
    if stop_distance <= 0:
        return

    if trade.side == "buy":
        best_price = max(c[2] for c in relevant_candles)
        favorable_move = best_price - trade.entry_price
        retrace = best_price - current_price
    else:
        best_price = min(c[3] for c in relevant_candles)
        favorable_move = trade.entry_price - best_price
        retrace = current_price - best_price

    if favorable_move <= 0:
        return  # henüz kâra geçmedik

    favorable_ratio = favorable_move / stop_distance

    # ------------------------------------------------------------------
    # KATMAN 1: Kademeli stop iyileştirme (BE + trailing)
    # ------------------------------------------------------------------
    if favorable_ratio >= min_ratio:
        # Uygulanabilecek EN YÜKSEK kâr kilitleme seviyesini bul
        target_stop = None
        for ratio_threshold, stop_offset_mult in config.PROFIT_LOCK_LEVELS:
            if favorable_ratio >= ratio_threshold:
                if trade.side == "buy":
                    candidate = trade.entry_price + (stop_distance * stop_offset_mult)
                else:
                    candidate = trade.entry_price - (stop_distance * stop_offset_mult)
                target_stop = candidate

        if target_stop is not None:
            _update_protective_stop(trade, target_stop, client)

    # ------------------------------------------------------------------
    # KATMAN 2: Sert kapatma (kâr erimesi)
    # ------------------------------------------------------------------
    if favorable_ratio >= min_ratio:
        retrace_ratio = retrace / favorable_move
        if retrace_ratio >= config.PROFIT_PROTECT_CLOSE_RETRACE:
            try:
                client.cancel_all_algo_orders(trade.symbol)
                client.close_position(trade.symbol, trade.side, trade.amount)
            except Exception as e:
                logger.error(f"[{trade.engine}] {trade.symbol}: kâr koruma kapatması başarısız: {e}")
                return

            pnl_usd = _estimate_pnl(trade, current_price)
            repository.close_trade(trade.id, current_price, pnl_usd, TradeStatus.CLOSED_MANUAL)
            new_virtual_balance = repository.update_virtual_balance(pnl_usd)

            kill_switch.record_realized_pnl(pnl_usd, new_virtual_balance - pnl_usd)
            kill_switch.record_engine_trade_result(trade.engine, is_win=(pnl_usd > 0))

            logger.info(
                f"[{trade.engine}] 🛡️ {trade.symbol}: kâr koruma - "
                f"%{favorable_ratio*100:.0f} kârdan %{retrace_ratio*100:.0f} geri çekilme | "
                f"PnL: {pnl_usd:.2f}$"
            )
            notifier.send_trade_update(
                trade, f"erken kapama (kâr koruma, %{favorable_ratio*100:.0f} kâr)"
            )
