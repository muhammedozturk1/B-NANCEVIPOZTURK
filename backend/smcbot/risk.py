"""Risk yönetimi: pozisyon boyutu, koruma kuralları ve performans istatistikleri.

Kaldıraç ile risk aynı şey değildir: 10x kaldıraç sadece ne kadar teminat
bağlanacağını belirler. Kaybedilecek para = pozisyon büyüklüğü x stop mesafesi.
Bu yüzden boyut stop mesafesinden hesaplanır.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import math

from . import db
from .binance_client import SymbolInfo


def safe_leverage(stop_frac: float, max_lev: int) -> int:
    """Likidasyon stoptan önce gelmesin: kaldıraç, stop mesafesinin ~1,5 katı + bakım marjı payına göre sınırlanır.
    Örnek: stop %4 -> 10x, %8 -> 7x, %14 -> 4x. Risk değişmez (miktar stop mesafesinden hesaplanır)."""
    if stop_frac <= 0:
        return max(1, int(max_lev))
    return max(1, min(int(max_lev), math.floor(1 / (stop_frac * 1.5 + 0.01))))

IST = ZoneInfo("Europe/Istanbul")


def size_position(balance: float, entry: float, stop: float, s: dict, sym: SymbolInfo | None) -> dict | None:
    if balance <= 0 or entry <= 0 or entry == stop:
        return None
    fee = s["taker_fee_pct"] / 100
    slip = s["slippage_pct"] / 100
    risk_amt = balance * s["risk_per_trade_pct"] / 100
    per_unit = abs(entry - stop) + entry * (2 * fee + slip)
    qty = risk_amt / per_unit
    lev = safe_leverage(abs(entry - stop) / entry, int(s["leverage"]))
    max_notional = balance * lev * 0.95 / max(1, int(s["max_open_positions"]))
    capped = False
    if qty * entry > max_notional:
        qty = max_notional / entry
        capped = True
    if sym is not None:
        qty = sym.round_qty(qty)
        if qty < float(sym.market_min_qty) or qty * entry < float(sym.min_notional):
            return None
    if qty <= 0:
        return None
    notional = qty * entry
    return {
        "qty": qty,
        "notional": notional,
        "margin": notional / lev,
        "risk_amount": qty * per_unit,
        "capped": capped,
        "leverage": lev,
    }


def ist_day(ms: int | None = None) -> str:
    dt = datetime.fromtimestamp((ms or time.time() * 1000) / 1000, tz=IST)
    return dt.strftime("%Y-%m-%d")


class RiskGuard:
    """Yeni işlem açmadan önce tüm koruma kurallarını kontrol eder."""

    def __init__(self, mode: str):
        self.mode = mode

    def _k(self, name: str) -> str:
        return f"risk:{self.mode}:{name}"

    def state(self) -> dict:
        return {
            "halted": db.kv_get(self._k("halted"), False),
            "halt_reason": db.kv_get(self._k("halt_reason"), ""),
            "cooldown_until": db.kv_get(self._k("cooldown_until"), 0),
            "peak_equity": db.kv_get(self._k("peak_equity"), 0.0),
            "day": db.kv_get(self._k("day"), ""),
            "day_start_balance": db.kv_get(self._k("day_start_balance"), 0.0),
        }

    def reset(self) -> None:
        db.kv_delete_prefix(f"risk:{self.mode}:")

    def unlock(self) -> None:
        db.kv_set(self._k("halted"), False)
        db.kv_set(self._k("halt_reason"), "")
        db.kv_set(self._k("cooldown_until"), 0)

    def update(self, balance: float, equity: float, now_ms: int, s: dict) -> str | None:
        """Her döngüde çağrılır. Yeni bir kilit oluşursa açıklamasını döner."""
        day = ist_day(now_ms)
        if db.kv_get(self._k("day")) != day:
            db.kv_set(self._k("day"), day)
            db.kv_set(self._k("day_start_balance"), balance)
        peak = max(db.kv_get(self._k("peak_equity"), 0.0) or 0.0, equity)
        db.kv_set(self._k("peak_equity"), peak)
        if peak > 0 and not db.kv_get(self._k("halted"), False):
            dd = (peak - equity) / peak * 100
            if dd >= s["max_drawdown_pct"]:
                reason = f"Zirveden %{dd:.1f} düşüş: maksimum drawdown limiti (%{s['max_drawdown_pct']:.0f}) aşıldı"
                db.kv_set(self._k("halted"), True)
                db.kv_set(self._k("halt_reason"), reason)
                return reason
        return None

    def daily_pnl(self, now_ms: int) -> float:
        day = ist_day(now_ms)
        total = 0.0
        for t in db.list_trades(self.mode, ("closed",), limit=500):
            if t.get("exit_time") and ist_day(t["exit_time"]) == day:
                total += t.get("pnl") or 0.0
        return total

    def register_close(self, now_ms: int, s: dict) -> str | None:
        """İşlem kapanınca art arda stop kontrolü."""
        recent = db.list_trades(self.mode, ("closed",), limit=max(1, int(s["max_consecutive_losses"])))
        n = int(s["max_consecutive_losses"])
        if n > 0 and len(recent) >= n and all((t.get("pnl") or 0) < 0 for t in recent[:n]):
            until = now_ms + int(s["cooldown_hours"] * 3_600_000)
            if db.kv_get(self._k("cooldown_until"), 0) < until:
                db.kv_set(self._k("cooldown_until"), until)
                return f"Art arda {n} zararlı işlem: {s['cooldown_hours']} saat soğuma"
        return None

    def can_open(self, side: str, symbol: str, open_trades: list[dict], now_ms: int, s: dict) -> tuple[bool, str]:
        st = self.state()
        if st["halted"]:
            return False, f"Risk kilidi: {st['halt_reason']}"
        if st["cooldown_until"] and now_ms < st["cooldown_until"]:
            kalan = (st["cooldown_until"] - now_ms) / 60000
            return False, f"Soğuma süresi ({kalan:.0f} dk kaldı)"
        dsb = st["day_start_balance"] or 0
        if dsb > 0:
            dpnl = self.daily_pnl(now_ms)
            if dpnl <= -dsb * s["daily_loss_limit_pct"] / 100:
                return False, f"Günlük zarar limiti doldu ({dpnl:.2f} USDT)"
        active = [t for t in open_trades if t["status"] == "open"]
        if len(active) >= int(s["max_open_positions"]):
            return False, f"Maksimum açık pozisyon ({s['max_open_positions']})"
        if sum(1 for t in active if t["side"] == side) >= int(s["max_same_direction"]):
            return False, f"Aynı yönde en fazla {s['max_same_direction']} pozisyon"
        if any(t["symbol"] == symbol for t in active):
            return False, "Bu coinde zaten pozisyon var"
        return True, ""


# ---------------------------------------------------------------------------
# İstatistikler
# ---------------------------------------------------------------------------
def compute_stats(trades: list[dict], start_balance: float | None = None) -> dict:
    closed = sorted([t for t in trades if t["status"] == "closed"], key=lambda t: t.get("exit_time") or 0)
    n = len(closed)
    out = {
        "trades": n, "wins": 0, "losses": 0, "winrate": 0.0, "net_pnl": 0.0, "gross_profit": 0.0,
        "gross_loss": 0.0, "profit_factor": None, "avg_r": 0.0, "expectancy_r": 0.0, "best_r": None,
        "worst_r": None, "avg_win_r": 0.0, "avg_loss_r": 0.0, "max_drawdown_pct": 0.0, "max_losing_streak": 0,
        "current_streak": 0, "avg_duration_min": 0.0, "fees": 0.0,
        "by_side": {}, "by_symbol": [], "r_hist": [], "by_reason": {},
    }
    if n == 0:
        return out
    rs = [t.get("r_multiple") or 0.0 for t in closed]
    pnls = [t.get("pnl") or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    out["wins"], out["losses"] = len(wins), len(losses)
    out["winrate"] = len(wins) / n * 100
    out["net_pnl"] = sum(pnls)
    out["gross_profit"] = sum(wins)
    out["gross_loss"] = sum(losses)
    out["profit_factor"] = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (None if not wins else 99.0)
    out["avg_r"] = sum(rs) / n
    out["expectancy_r"] = out["avg_r"]
    out["best_r"], out["worst_r"] = max(rs), min(rs)
    wr = [r for r, p in zip(rs, pnls) if p > 0]
    lr = [r for r, p in zip(rs, pnls) if p <= 0]
    out["avg_win_r"] = sum(wr) / len(wr) if wr else 0.0
    out["avg_loss_r"] = sum(lr) / len(lr) if lr else 0.0
    out["fees"] = sum(t.get("fees") or 0 for t in closed)

    # drawdown (kapanan işlemlerle bakiye eğrisi)
    bal = start_balance if start_balance else max(1.0, abs(sum(pnls)) * 2)
    peak, mdd = bal, 0.0
    for p in pnls:
        bal += p
        peak = max(peak, bal)
        if peak > 0:
            mdd = max(mdd, (peak - bal) / peak * 100)
    out["max_drawdown_pct"] = mdd

    streak = best_streak = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        best_streak = max(best_streak, streak)
    out["max_losing_streak"] = best_streak
    cur = 0
    for p in reversed(pnls):
        if cur == 0:
            cur = 1 if p > 0 else -1
        elif (p > 0) == (cur > 0):
            cur += 1 if cur > 0 else -1
        else:
            break
    out["current_streak"] = cur

    durs = [(t["exit_time"] - t["entry_time"]) / 60000 for t in closed if t.get("exit_time") and t.get("entry_time")]
    out["avg_duration_min"] = sum(durs) / len(durs) if durs else 0.0

    for side in ("LONG", "SHORT"):
        st = [t for t in closed if t["side"] == side]
        if st:
            w = sum(1 for t in st if (t.get("pnl") or 0) > 0)
            out["by_side"][side] = {"trades": len(st), "winrate": w / len(st) * 100,
                                    "pnl": sum(t.get("pnl") or 0 for t in st),
                                    "avg_r": sum(t.get("r_multiple") or 0 for t in st) / len(st)}
    sym: dict[str, dict] = {}
    for t in closed:
        d = sym.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "pnl": 0.0, "wins": 0})
        d["trades"] += 1
        d["pnl"] += t.get("pnl") or 0
        d["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
    out["by_symbol"] = sorted(sym.values(), key=lambda d: -d["pnl"])
    for t in closed:
        k = t.get("exit_reason") or "?"
        out["by_reason"][k] = out["by_reason"].get(k, 0) + 1
    buckets = [(-99, -0.75, "≤ -1R"), (-0.75, -0.05, "-1R..0"), (-0.05, 0.05, "~0 (başabaş)"),
               (0.05, 1.0, "0..1R"), (1.0, 2.0, "1..2R"), (2.0, 3.0, "2..3R"), (3.0, 99, "3R+")]
    out["r_hist"] = [{"label": lbl, "count": sum(1 for r in rs if lo <= r < hi)} for lo, hi, lbl in buckets]
    return out
