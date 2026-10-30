"""
FONLAMA MOTORU (paper) - saatte bir çalışır (her saatin 5. dakikası).

Her döngüde:
  1) TAHSİLAT : açık pozisyonların son döngüden beri gerçekleşen fonlama ödemelerini
                (gerçek Binance verisi) pozisyona ekler
  2) ÇIKIŞ    : fonlama düştüyse / negatife döndüyse / fiyat çok oynadıysa kapatır
  3) GİRİŞ    : son 3 günde yüksek ve istikrarlı fonlama ödeyen coinlere girer
  4) ANLIK K/Z: dashboard için açık pozisyonların güncel net K/Z'sini kaydeder
"""
import logging
import threading
import time
from datetime import datetime

from backend import config
from backend.db import repository
from backend.db import status as bot_status
from backend.db.database import get_session
from backend.db.models import FundingPosition
from backend.funding import strategy as fs
from backend.telegram import notifier

logger = logging.getLogger("funding_engine")
_market = None
_lock = threading.Lock()   # iki döngü aynı anda çalışmasın (çift pozisyon açılmasın)


def get_market():
    global _market
    if _market is None:
        from backend.funding.market import FundingMarket
        _market = FundingMarket()
    return _market


def _now_ms() -> int:
    return int(time.time() * 1000)


def _open_positions(session):
    return session.query(FundingPosition).filter(FundingPosition.status == "open").all()


def _capital_per_notional() -> float:
    return 1.0 + 1.0 / config.FUNDING["perp_leverage"]


# ----------------------------------------------------------------------
# 1) TAHSİLAT
# ----------------------------------------------------------------------
def accrue(market, session):
    for pos in _open_positions(session):
        try:
            events = market.history(pos.symbol, pos.last_funding_ts + 1)
        except Exception as e:
            logger.warning(f"[funding] {pos.symbol}: fonlama geçmişi alınamadı: {e}")
            continue
        new = [(t, r, m) for t, r, m in events if t > pos.last_funding_ts]
        if not new:
            continue
        try:
            _, perp_now = market.prices(pos.symbol)
        except Exception:
            perp_now = pos.perp_entry
        total = 0.0
        for t, rate, mark in new:
            payment = rate * pos.amount * (mark or perp_now)   # short: pozitif oran -> gelir
            total += payment
            pos.funding_payments = (pos.funding_payments or 0) + 1
            pos.last_funding_ts = t
        pos.funding_usd = (pos.funding_usd or 0.0) + total
        logger.info(f"[funding] 💰 {pos.symbol}: {len(new)} ödeme, {total:+.4f}$ "
                    f"(toplam {pos.funding_usd:+.4f}$)")
    session.commit()


# ----------------------------------------------------------------------
# 2) ÇIKIŞ
# ----------------------------------------------------------------------
def _close(market, session, pos, reason: str):
    c = config.FUNDING
    spot_px, perp_px = market.prices(pos.symbol)
    spot_exit = spot_px * (1 - c["slippage"])
    perp_exit = perp_px * (1 + c["slippage"])
    exit_fees = pos.amount * (spot_exit * c["spot_fee"] + perp_exit * c["perp_fee"])
    basis_pnl = (spot_exit - pos.spot_entry) * pos.amount + (pos.perp_entry - perp_exit) * pos.amount
    pnl = basis_pnl + (pos.funding_usd or 0) - (pos.entry_fees_usd or 0) - exit_fees

    pos.status, pos.close_reason = "closed", reason
    pos.spot_exit, pos.perp_exit, pos.exit_fees_usd = spot_exit, perp_exit, exit_fees
    pos.pnl_usd, pos.unrealized_pnl_usd = pnl, None
    pos.closed_at = datetime.utcnow()
    session.commit()

    balance = repository.update_virtual_balance(pnl)
    days = (pos.closed_at - pos.opened_at).total_seconds() / 86400
    logger.info(f"[funding] 📌 {pos.symbol} kapandı ({reason}) | {days:.1f} gün | fonlama "
                f"{pos.funding_usd:+.4f}$ | fiyat farkı {basis_pnl:+.4f}$ | net {pnl:+.4f}$ | kasa {balance:.2f}$")
    notifier.send_funding_closed(pos, days, reason)


def process_exits(market, session):
    now = _now_ms()
    lookback = config.FUNDING["lookback_hours"]
    for pos in _open_positions(session):
        try:
            events = [(t, r) for t, r, _ in market.history(pos.symbol, now - (lookback + 24) * fs.HOUR_MS)]
            stats = fs.trailing_stats(events, now)
            _, perp_px = market.prices(pos.symbol)
            move = perp_px / pos.perp_entry - 1
            held_h = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            reason = fs.exit_reason(stats, move, held_h)
            if reason:
                _close(market, session, pos, reason)
        except Exception as e:
            logger.error(f"[funding] {pos.symbol} çıkış kontrolünde hata: {e}")


# ----------------------------------------------------------------------
# 3) GİRİŞ
# ----------------------------------------------------------------------
def process_entries(market, session) -> dict:
    """Uygun coinlere girer. Dönüş: dashboard için tarama özeti (neden girildi / girilmedi)."""
    c = config.FUNDING
    diag = {"candidates": 0, "prefiltered": 0, "checked": 0, "opened": [], "top": [], "note": None}
    open_pos = _open_positions(session)
    if len(open_pos) >= c["max_positions"]:
        diag["note"] = "pozisyon limiti dolu"
        return diag
    balance = repository.get_virtual_balance()
    used = sum(p.notional_usd for p in open_pos) * _capital_per_notional()
    held = {p.symbol for p in open_pos}

    rates = market.current_rates()
    candidates = [s for s in market.candidates() if s not in held]
    diag["candidates"] = len(candidates)
    # Ön eleme: güncel oranı düşük olanlar için geçmişe bakmaya gerek yok.
    # (Aralık bilinmediği için 8 saat varsayımı -> 4h/1h coinlerde APR düşük çıkar; eşik gevşek tutuldu.)
    pool = [s for s in candidates if fs.to_apr(rates.get(s, 0.0), 8.0) >= c["entry_last_min_apr"] / 4]
    pool.sort(key=lambda s: -rates.get(s, 0.0))
    diag["prefiltered"] = len(pool)

    now = _now_ms()
    scored, seen = [], []
    for sym in pool[: c["max_history_checks"]]:
        try:
            events = [(t, r) for t, r, _ in market.history(sym, now - (c["lookback_hours"] + 24) * fs.HOUR_MS)]
            stats = fs.trailing_stats(events, now)
        except Exception as e:
            logger.warning(f"[funding] {sym}: geçmiş alınamadı: {e}")
            continue
        diag["checked"] += 1
        reason = fs.entry_reject_reason(stats)
        seen.append({"symbol": sym, "avg_apr": round(stats["avg_apr"], 4), "last_apr": round(stats["last_apr"], 4),
                     "interval_h": round(stats["interval_h"], 1), "ok": reason is None, "reason": reason,
                     "breakeven_days": round(min(fs.breakeven_days(stats["avg_apr"]), 9999), 1)})
        if fs.entry_ok(stats):
            scored.append((stats["avg_apr"], sym, stats))
    scored.sort(key=lambda x: -x[0])
    seen.sort(key=lambda x: -x["avg_apr"])
    diag["top"] = seen[:8]

    for avg_apr, sym, stats in scored:
        if len(open_pos) >= c["max_positions"]:
            break
        notional = balance * c["position_pct"]
        need = notional * _capital_per_notional()
        if used + need > balance * 0.95:
            logger.info("[funding] serbest sermaye yetersiz, yeni pozisyon açılmadı.")
            diag["note"] = "serbest sermaye yetersiz"
            break
        try:
            spot_px, perp_px = market.prices(sym)
        except Exception as e:
            logger.warning(f"[funding] {sym}: fiyat alınamadı: {e}")
            continue
        spot_entry = spot_px * (1 + c["slippage"])
        perp_entry = perp_px * (1 - c["slippage"])
        amount = notional / spot_entry
        entry_fees = amount * (spot_entry * c["spot_fee"] + perp_entry * c["perp_fee"])
        pos = FundingPosition(
            symbol=sym, mode=c["mode"], status="open", amount=amount, notional_usd=notional,
            spot_entry=spot_entry, perp_entry=perp_entry, entry_apr=avg_apr,
            funding_usd=0.0, funding_payments=0, last_funding_ts=now,
            entry_fees_usd=entry_fees, unrealized_pnl_usd=-entry_fees,
            opened_at=datetime.utcnow(),
        )
        session.add(pos)
        session.commit()
        open_pos.append(pos)
        used += need
        diag["opened"].append(sym)
        logger.info(f"[funding] ✅ {sym} açıldı | nominal {notional:.2f}$ | 3 günlük ort. yıllık fonlama "
                    f"%{avg_apr * 100:.1f} | ödeme aralığı {stats['interval_h']:.0f} saat | "
                    f"maliyet ~{fs.breakeven_days(avg_apr):.1f} günde çıkar")
        notifier.send_funding_opened(pos, stats)

    if not scored and diag["note"] is None:
        best = diag["top"][0] if diag["top"] else None
        diag["note"] = ("giriş şartını sağlayan coin yok" +
                        (f" (en iyi: {best['symbol']} %{best['avg_apr'] * 100:.1f})" if best else ""))
    return diag


# ----------------------------------------------------------------------
# 4) ANLIK K/Z
# ----------------------------------------------------------------------
def snapshot(market, session):
    c = config.FUNDING
    for pos in _open_positions(session):
        try:
            spot_px, perp_px = market.prices(pos.symbol)
        except Exception:
            continue
        exit_fees = pos.amount * (spot_px * (c["spot_fee"] + c["slippage"]) + perp_px * (c["perp_fee"] + c["slippage"]))
        basis = (spot_px - pos.spot_entry) * pos.amount + (pos.perp_entry - perp_px) * pos.amount
        pos.unrealized_pnl_usd = basis + (pos.funding_usd or 0) - (pos.entry_fees_usd or 0) - exit_fees
    session.commit()


def run_cycle():
    if not config.FUNDING["enabled"]:
        return
    if not _lock.acquire(blocking=False):
        logger.info("[funding] önceki döngü hâlâ çalışıyor, atlandı.")
        return
    try:
        _run_cycle_locked()
    finally:
        _lock.release()


def _run_cycle_locked():
    market = get_market()
    session = get_session()
    try:
        accrue(market, session)
        process_exits(market, session)
        diag = process_entries(market, session)
        snapshot(market, session)
        n = len(_open_positions(session))
        diag["open_positions"] = n
        diag["max_positions"] = config.FUNDING["max_positions"]
        diag["entry_min_apr"] = config.FUNDING["entry_min_apr"]
        bot_status.mark_ok("funding", diag)
        logger.info(f"[funding] döngü tamam | açık pozisyon: {n}/{config.FUNDING['max_positions']} | "
                    f"{diag['candidates']} aday, {diag['checked']} detaylı incelendi"
                    + (f" | {diag['note']}" if diag.get("note") else ""))
    finally:
        session.close()
