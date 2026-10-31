"""OTE, displacement ve FVG."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.smc.core.types import Candle
from backend.smc.strategy.structure import Direction


@dataclass(frozen=True, slots=True)
class OTEZone:
    direction: Direction
    leg_start: float  # sweep extreme (long için low)
    leg_end: float  # impulse extreme (long için high)
    low: float
    high: float
    preferred: float

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def touched_by(self, c: Candle) -> bool:
        return c.low <= self.high and c.high >= self.low

    @property
    def leg_range(self) -> float:
        return abs(self.leg_end - self.leg_start)


def compute_ote(direction: Direction, leg_start: float, leg_end: float, lo: float = 0.62, hi: float = 0.79,
                pref: float = 0.705) -> OTEZone:
    """%62–%79 retracement, tercih edilen %70.5.

    Long: leg_start=low, leg_end=high. Retracement aşağı doğru ölçülür.
    Short: leg_start=high, leg_end=low.
    """
    r = abs(leg_end - leg_start)
    if direction == "bullish":
        if leg_end <= leg_start:
            raise ValueError("bullish leg: leg_end > leg_start olmalı")
        return OTEZone(direction, leg_start, leg_end, leg_end - r * hi, leg_end - r * lo, leg_end - r * pref)
    if leg_end >= leg_start:
        raise ValueError("bearish leg: leg_end < leg_start olmalı")
    return OTEZone(direction, leg_start, leg_end, leg_end + r * lo, leg_end + r * hi, leg_end + r * pref)


def average_body(candles: Sequence[Candle], end: int, period: int) -> float:
    window = candles[max(0, end - period): end]
    if not window:
        return 0.0
    return sum(c.body for c in window) / len(window)


def average_volume(candles: Sequence[Candle], end: int, period: int) -> float:
    window = candles[max(0, end - period): end]
    if not window:
        return 0.0
    return sum(c.volume for c in window) / len(window)


def is_displacement(candles: Sequence[Candle], i: int, direction: Direction, body_mult: float = 1.5,
                    period: int = 20, close_location: float = 0.7, vol_mult: float = 1.0) -> bool:
    """Güçlü fiyat hareketi: gövde >= mult × ortalama gövde, yöne uygun kapanış lokasyonu, hacim >= ortalama."""
    c = candles[i]
    avg_b = average_body(candles, i, period)
    if avg_b <= 0 or c.body < body_mult * avg_b:
        return False
    if direction == "bullish" and (not c.bullish or c.close_location < close_location):
        return False
    if direction == "bearish" and (not c.bearish or c.close_location > 1 - close_location):
        return False
    avg_v = average_volume(candles, i, period)
    return avg_v <= 0 or c.volume >= vol_mult * avg_v


def find_displacement(candles: Sequence[Candle], start: int, end: int, direction: Direction, **kw) -> int | None:
    for i in range(max(start, 1), min(end, len(candles) - 1) + 1):
        if is_displacement(candles, i, direction, **kw):
            return i
    return None


@dataclass(frozen=True, slots=True)
class FVG:
    direction: Direction
    index: int  # orta mum
    time: int
    low: float
    high: float


def find_fvgs(candles: Sequence[Candle], start: int, end: int, direction: Direction) -> list[FVG]:
    """Bullish FVG: Candle1.high < Candle3.low ; Bearish FVG: Candle1.low > Candle3.high."""
    out = []
    for j in range(max(0, start), min(end, len(candles) - 1) - 1):
        c1, c2, c3 = candles[j], candles[j + 1], candles[j + 2]
        if direction == "bullish" and c1.high < c3.low:
            out.append(FVG(direction, j + 1, c2.open_time, c1.high, c3.low))
        elif direction == "bearish" and c1.low > c3.high:
            out.append(FVG(direction, j + 1, c2.open_time, c3.high, c1.low))
    return out
