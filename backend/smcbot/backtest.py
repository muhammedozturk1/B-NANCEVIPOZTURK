"""Geçmiş test: aynı strateji kodunu geçmiş mumlar üzerinde çalıştırır.

Sınırlamalar (dürüstçe):
- Geçmiş likidasyon akışı, emir defteri ve funding verisi olmadığı için sadece yapı + CVD
  puanı kullanılır (bu yüzden ayrı bir eşik: backtest_min_score).
- Aynı mum içinde hem stop hem hedef görüldüyse STOP varsayılır (kötümser).
- Eşzamanlı pozisyon limitleri yaklaşık hesaplanır.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from .binance_client import parse_klines
from .config import INTERVAL_MS
from .risk import compute_stats
from .smc import Candles
from .strategy import SymbolAnalyzer


def simulate_symbol(symbol: str, ltf: Candles, htf: Candles, s: dict, start_ms: int, diag: dict | None = None) -> list[dict]:
    an = SymbolAnalyzer(symbol, ltf, htf)
    c = ltf
    fee = s["taker_fee_pct"] / 100
    slip = s["slippage_pct"] / 100
    f1 = s["tp1_close_pct"] / 100
    out: list[dict] = []
    pending = None
    pos = None
    dg = diag if diag is not None else {}
    for k in ("signals", "filled", "expired", "ran_away"):
        dg.setdefault(k, 0)
    first = 120
    while first < len(c) and c.time[first] < start_ms:
        first += 1

    for t in range(first, len(c)):
        h, lo = c.high[t], c.low[t]
        bar_close_time = int(c.time[t] + c.interval_ms)
        if pending is not None:
            sg = pending
            L = sg.side == "LONG"
            touched = lo <= sg.entry if L else h >= sg.entry
            if s.get("entry_mode") == "market":
                ok_open = (sg.stop < c.open[t] < sg.tp1) if L else (sg.tp1 < c.open[t] < sg.stop)
                if ok_open:
                    sg.entry = float(c.open[t])  # sonraki mumun açılışında piyasa emri
                    touched = True
                else:
                    pending = None
                    dg["ran_away"] += 1
                    continue
            if c.time[t] >= sg.expiry_time:
                pending = None
                dg["expired"] += 1
            elif touched:
                dg["filled"] += 1
                pos = {"sig": sg, "entry": sg.entry * (1 + slip * (1 if L else -1)), "sl": sg.stop,
                       "tp1_hit": False, "entry_time": int(c.time[t]), "mfe": 0.0, "mae": 0.0}
                pending = None
            elif (h >= sg.tp1) if L else (lo <= sg.tp1):
                pending = None  # girişe gelmeden kaçtı
                dg["ran_away"] += 1
        if pos is not None:
            sg = pos["sig"]
            L = sg.side == "LONG"
            d = 1 if L else -1
            risk_u = abs(pos["entry"] - sg.stop)
            exit_px = reason = None
            fav = (h - pos["entry"]) * d if L else (pos["entry"] - lo)
            adv = (pos["entry"] - lo) if L else (h - pos["entry"])
            pos["mfe"] = max(pos["mfe"], fav / risk_u)
            pos["mae"] = max(pos["mae"], adv / risk_u)
            if (lo <= pos["sl"]) if L else (h >= pos["sl"]):
                exit_px = pos["sl"] * (1 - slip * d)
                reason = "Başabaş stop" if pos["tp1_hit"] else "Stop"
            else:
                if not pos["tp1_hit"] and ((h >= sg.tp1) if L else (lo <= sg.tp1)):
                    pos["tp1_hit"] = True
                    pos["sl"] = pos["entry"] * (1 + 2 * fee * d)
                if (h >= sg.tp2) if L else (lo <= sg.tp2):
                    exit_px, reason = sg.tp2, "TP2 hedef"
            if exit_px is not None:
                e = pos["entry"]
                if pos["tp1_hit"]:
                    gross = f1 * (sg.tp1 - e) * d + (1 - f1) * (exit_px - e) * d
                    avg_exit = f1 * sg.tp1 + (1 - f1) * exit_px
                else:
                    gross = (exit_px - e) * d
                    avg_exit = exit_px
                fees = (e + avg_exit) * fee
                r = (gross - fees) / risk_u
                out.append({
                    "symbol": symbol, "side": sg.side, "status": "closed", "score": sg.score,
                    "setup_time": sg.setup_time, "entry_time": pos["entry_time"], "exit_time": bar_close_time,
                    "entry_price": e, "exit_price": avg_exit, "sl": sg.stop, "sl_initial": sg.stop, "tp1": sg.tp1,
                    "tp2": sg.tp2, "tp1_hit": int(pos["tp1_hit"]), "r_multiple": r, "fee_r": fees / risk_u, "exit_reason": reason,
                    "mfe": pos["mfe"], "mae": pos["mae"], "reasons": sg.reasons, "annotations": sg.annotations,
                })
                pos = None
        if pos is None and pending is None:
            sig, _ = an.evaluate(t, s, None, min_score=s["backtest_min_score"])
            if sig is not None:
                pending = sig
                dg["signals"] += 1
    return out


def portfolio(trades: list[dict], s: dict, start_balance: float = 1000.0) -> dict:
    trades = sorted(trades, key=lambda x: x["exit_time"])
    bal = start_balance
    curve = []
    open_until: list[int] = []
    kept = []
    for t in trades:
        # eşzamanlı pozisyon sınırı (yaklaşık)
        open_until = [x for x in open_until if x > t["entry_time"]]
        if len(open_until) >= int(s["max_open_positions"]):
            continue
        open_until.append(t["exit_time"])
        risk_amt = bal * s["risk_per_trade_pct"] / 100
        pnl = risk_amt * t["r_multiple"]
        bal += pnl
        t = dict(t, pnl=pnl, fees=risk_amt * t.get("fee_r", 0.0), risk_amount=risk_amt, id=len(kept) + 1)
        kept.append(t)
        curve.append({"ts": t["exit_time"], "balance": bal, "equity": bal})
    stats = compute_stats(kept, start_balance)
    stats["final_balance"] = bal
    stats["return_pct"] = (bal / start_balance - 1) * 100
    return {"trades": kept, "curve": curve, "stats": stats}


async def run(md, s: dict, days: int, symbols: list[str], progress: Callable[[int, int, str], None]) -> dict:
    ltf_i, htf_i = s["ltf"], s["htf"]
    now = md.clock.now_ms()
    start = now - days * 86_400_000
    warm_l = 300 * INTERVAL_MS[ltf_i]
    warm_h = 200 * INTERVAL_MS[htf_i]
    all_trades: list[dict] = []
    errors = []
    diag: dict = {}
    for k, sym in enumerate(symbols):
        progress(k, len(symbols), sym)
        try:
            rl = await md.rest.klines_range(sym, ltf_i, start - warm_l, now)
            rh = await md.rest.klines_range(sym, htf_i, start - warm_h, now)
            rl = [x for x in rl if int(x[6]) < now]
            rh = [x for x in rh if int(x[6]) < now]
            if len(rl) < 400 or len(rh) < 100:
                continue
            ltf = Candles.from_cols(parse_klines(rl), INTERVAL_MS[ltf_i])
            htf = Candles.from_cols(parse_klines(rh), INTERVAL_MS[htf_i])
            all_trades += await asyncio.to_thread(simulate_symbol, sym, ltf, htf, s, start, diag)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}: {e}")
    progress(len(symbols), len(symbols), "")
    res = portfolio(all_trades, s)
    res["errors"] = errors
    res["diag"] = diag
    res["params"] = {"days": days, "symbols": symbols, "min_score": s["backtest_min_score"],
                     "risk_pct": s["risk_per_trade_pct"], "ltf": ltf_i, "htf": htf_i}
    return res
