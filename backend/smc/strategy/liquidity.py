"""Liquidity seviyeleri ve liquidity sweep tespiti.

Sweep tanımı: fiyat seviyeyi alır (wick ile geçer), breakout'u sürdüremez
(`reclaim_bars` içinde seviyenin diğer tarafında kapanır) VE ardından karşı yönde
kapanışla doğrulanmış structure kırılımı (BOS/CHoCH) üretir.
Sadece seviyeye dokunmak sweep değildir.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence

from backend.smc.core.types import Candle
from backend.smc.strategy.structure import Direction, StructureEvent, StructureState, Swing


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    price: float
    side: Literal["buy", "sell"]  # buy-side: highs üstü, sell-side: lows altı
    kind: str  # swing_internal, swing_external, equal, pdh, pdl, session
    known_from: int  # bu zamandan (ms) itibaren bilinen seviye
    strength: int  # 1 internal, 2 external / major


@dataclass(frozen=True, slots=True)
class Sweep:
    direction: Direction  # bullish: sell-side alındı -> long setup
    index: int
    time: int
    level: LiquidityLevel
    extreme: float
    reclaim_index: int
    confirm: StructureEvent


def swing_levels(swings: Sequence[Swing], candles: Sequence[Candle], external: bool) -> list[LiquidityLevel]:
    out = []
    for s in swings:
        if s.confirm_index >= len(candles):
            continue
        known = candles[s.confirm_index].close_time
        out.append(LiquidityLevel(s.price, "buy" if s.kind == "high" else "sell",
                                  "swing_external" if external else "swing_internal", known, 2 if external else 1))
    return out


def equal_levels(swings: Sequence[Swing], candles: Sequence[Candle], tolerance: float) -> list[LiquidityLevel]:
    out = []
    for kind, side in (("high", "buy"), ("low", "sell")):
        pts = [s for s in swings if s.kind == kind and s.confirm_index < len(candles)]
        for i in range(len(pts)):
            for j in range(i + 1, min(i + 6, len(pts))):
                a, b = pts[i], pts[j]
                if abs(a.price - b.price) / a.price <= tolerance:
                    price = max(a.price, b.price) if kind == "high" else min(a.price, b.price)
                    known = candles[max(a.confirm_index, b.confirm_index)].close_time
                    out.append(LiquidityLevel(price, side, "equal", known, 2))
    return out


def _day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def previous_day_levels(candles: Sequence[Candle]) -> list[LiquidityLevel]:
    if not candles:
        return []
    days: dict[str, list[Candle]] = {}
    for c in candles:
        days.setdefault(_day_key(c.open_time), []).append(c)
    keys = sorted(days)
    if len(keys) < 2:
        return []
    prev = days[keys[-2]]
    known = days[keys[-1]][0].open_time
    return [LiquidityLevel(max(c.high for c in prev), "buy", "pdh", known, 2),
            LiquidityLevel(min(c.low for c in prev), "sell", "pdl", known, 2)]


def previous_session_levels(candles: Sequence[Candle], sessions: list[tuple[int, int]]) -> list[LiquidityLevel]:
    """Son tamamlanmış seans (UTC saat aralığı) high/low."""
    if not candles:
        return []

    def session_id(ms: int) -> tuple[str, int]:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        for idx, (a, b) in enumerate(sessions):
            if a <= dt.hour < b:
                return dt.strftime("%Y-%m-%d"), idx
        return dt.strftime("%Y-%m-%d"), -1

    groups: list[tuple[tuple[str, int], list[Candle]]] = []
    for c in candles:
        sid = session_id(c.open_time)
        if groups and groups[-1][0] == sid:
            groups[-1][1].append(c)
        else:
            groups.append((sid, [c]))
    if len(groups) < 2:
        return []
    prev = groups[-2][1]
    known = groups[-1][1][0].open_time
    return [LiquidityLevel(max(c.high for c in prev), "buy", "session", known, 2),
            LiquidityLevel(min(c.low for c in prev), "sell", "session", known, 2)]


def detect_sweep(candles: Sequence[Candle], levels: Sequence[LiquidityLevel], structure: StructureState,
                 direction: Direction, lookback: int = 36, reclaim_bars: int = 2) -> Sweep | None:
    """En güncel geçerli sweep'i döndürür.

    bullish (long): sell-side seviye wick ile alınır, reclaim_bars içinde üstünde kapanılır,
    sonra bullish BOS/CHoCH gelir. bearish: simetrik.
    """
    n = len(candles)
    if n < 5:
        return None
    side = "sell" if direction == "bullish" else "buy"
    lvls = [l for l in levels if l.side == side]
    best: Sweep | None = None
    for k in range(n - 1, max(-1, n - 1 - lookback), -1):
        ck = candles[k]
        for lvl in lvls:
            if lvl.known_from > ck.open_time:
                continue
            taken = ck.low < lvl.price if direction == "bullish" else ck.high > lvl.price
            if not taken:
                continue
            # seviye bu mumdan önce zaten alınmış mı? (alınmışsa artık likidite değil)
            already = False
            for j in range(k - 1, -1, -1):
                cj = candles[j]
                if cj.close_time <= lvl.known_from:
                    break
                if (direction == "bullish" and cj.low < lvl.price) or (direction == "bearish" and cj.high > lvl.price):
                    already = True
                    break
            if already:
                continue
            reclaim = None
            for r in range(k, min(n, k + reclaim_bars + 1)):
                cr = candles[r]
                if (direction == "bullish" and cr.close > lvl.price) or (direction == "bearish" and cr.close < lvl.price):
                    reclaim = r
                    break
            if reclaim is None:
                continue
            confirms = [e for e in structure.events_after(k, direction)]
            if not confirms:
                continue
            confirm = confirms[0]
            seg = candles[k: confirm.index + 1]
            extreme = min(c.low for c in seg) if direction == "bullish" else max(c.high for c in seg)
            cand = Sweep(direction, k, ck.open_time, lvl, extreme, reclaim, confirm)
            if best is None or lvl.strength > best.level.strength:
                best = cand
        if best is not None:
            return best
    return None
