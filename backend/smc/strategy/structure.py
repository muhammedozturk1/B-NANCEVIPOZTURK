"""Market structure: swing high/low, BOS ve CHoCH.

Kurallar (şartname):
- Swing High: önceki `left` ve sonraki `right` kapanmış mumun high'ından kesin olarak yüksek high.
- Swing Low: önceki `left` ve sonraki `right` kapanmış mumun low'undan kesin olarak düşük low.
- Bir swing, sağındaki `right` mum kapanınca (confirm_index = index + right) bilinir; daha önce kullanılmaz.
- BOS/CHoCH: seviye mum KAPANIŞI ile kırılmalıdır. Sadece wick kırılması kırılım değildir.
- CHoCH: mevcut trendin tersi yöndeki geçerli swing seviyesinin kapanışla kırılması.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from backend.smc.core.types import Candle

Direction = Literal["bullish", "bearish"]


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    price: float
    kind: Literal["high", "low"]
    time: int
    confirm_index: int


@dataclass(frozen=True, slots=True)
class StructureEvent:
    index: int
    time: int
    kind: Literal["BOS", "CHOCH"]
    direction: Direction
    level: float
    swing_index: int


@dataclass(slots=True)
class StructureState:
    trend: Direction | None
    events: list[StructureEvent] = field(default_factory=list)
    swings: list[Swing] = field(default_factory=list)

    def events_after(self, index: int, direction: Direction | None = None) -> list[StructureEvent]:
        return [e for e in self.events if e.index >= index and (direction is None or e.direction == direction)]

    def last_event(self) -> StructureEvent | None:
        return self.events[-1] if self.events else None


def find_swings(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    out: list[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        h, l = candles[i].high, candles[i].low
        if all(h > candles[j].high for j in range(i - left, i)) and \
                all(h > candles[j].high for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, h, "high", candles[i].open_time, i + right))
        if all(l < candles[j].low for j in range(i - left, i)) and \
                all(l < candles[j].low for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, l, "low", candles[i].open_time, i + right))
    return out


def analyze_structure(candles: Sequence[Candle], left: int = 2, right: int = 2) -> StructureState:
    swings = find_swings(candles, left, right)
    by_confirm: dict[int, list[Swing]] = {}
    for s in swings:
        by_confirm.setdefault(s.confirm_index, []).append(s)

    trend: Direction | None = None
    active_high: Swing | None = None
    active_low: Swing | None = None
    events: list[StructureEvent] = []

    for i, c in enumerate(candles):
        if active_high is not None and c.close > active_high.price:
            kind = "CHOCH" if trend == "bearish" else "BOS"
            events.append(StructureEvent(i, c.open_time, kind, "bullish", active_high.price, active_high.index))
            trend = "bullish"
            active_high = None
        if active_low is not None and c.close < active_low.price:
            kind = "CHOCH" if trend == "bullish" else "BOS"
            events.append(StructureEvent(i, c.open_time, kind, "bearish", active_low.price, active_low.index))
            trend = "bearish"
            active_low = None
        for s in by_confirm.get(i, []):
            if s.kind == "high":
                active_high = s
            else:
                active_low = s
    return StructureState(trend=trend, events=events, swings=swings)


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return candles[-1].range if candles else 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    window = trs[-period:]
    return sum(window) / len(window)
