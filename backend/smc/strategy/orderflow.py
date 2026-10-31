"""Synthetic Delta / Synthetic CVD ve OI bağlamı.

Delta = AggressiveBuyVolume - AggressiveSellVolume
CVD(t) = CVD(t-1) + Delta(t)
Bu veriler Binance trade / kline taker verisinden türetilir; gerçek exchange footprint DEĞİLDİR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from backend.smc.core.types import Candle


def synthetic_cvd(candles: Sequence[Candle], window: int = 50) -> list[float]:
    """Son `window` kapanmış mum üzerinde rolling Synthetic CVD (pencere başında 0'dan başlar)."""
    cvd, out = 0.0, []
    for c in candles[-window:]:
        cvd += c.delta
        out.append(cvd)
    return out


def slope(values: Sequence[float]) -> float:
    """En küçük kareler eğimi (değer/bar)."""
    n = len(values)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def cvd_bias(candles: Sequence[Candle], window: int = 50, lookback: int = 20) -> float:
    """CVD eğimini ortalama hacme göre normalize eder. >0 yükselen, <0 düşen."""
    cvd = synthetic_cvd(candles, window)
    if len(cvd) < 3:
        return 0.0
    seg = cvd[-lookback:]
    avg_vol = sum(c.volume for c in candles[-window:]) / max(1, len(candles[-window:]))
    return slope(seg) / avg_vol if avg_vol > 0 else 0.0


def recent_delta(candles: Sequence[Candle], n: int = 3) -> float:
    return sum(c.delta for c in candles[-n:])


@dataclass
class AggTradeDeltaAggregator:
    """aggTrade akışından mum bazlı delta. m=True -> alıcı maker -> agresif SATIŞ."""

    bucket_ms: int
    buckets: dict[int, float] = field(default_factory=dict)

    def add(self, trade_time: int, qty: float, buyer_is_maker: bool) -> None:
        b = trade_time - trade_time % self.bucket_ms
        self.buckets[b] = self.buckets.get(b, 0.0) + (-qty if buyer_is_maker else qty)

    def delta(self, bucket_start: int) -> float:
        return self.buckets.get(bucket_start, 0.0)

    def prune(self, keep: int = 200) -> None:
        for k in sorted(self.buckets)[:-keep]:
            del self.buckets[k]


@dataclass
class OISample:
    ts: int
    oi: float
    price: float


class OpenInterestTracker:
    def __init__(self, maxlen: int = 50):
        self.samples: dict[str, list[OISample]] = {}
        self.maxlen = maxlen

    def add(self, symbol: str, ts: int, oi: float, price: float) -> None:
        lst = self.samples.setdefault(symbol, [])
        lst.append(OISample(ts, oi, price))
        del lst[:-self.maxlen]

    def change(self, symbol: str, horizon_ms: int = 15 * 60_000) -> tuple[float, float] | None:
        """(oi_change_pct, price_change_pct) son `horizon_ms` içinde."""
        lst = self.samples.get(symbol) or []
        if len(lst) < 2:
            return None
        last = lst[-1]
        ref = next((s for s in lst if last.ts - s.ts <= horizon_ms), lst[0])
        if ref is last or ref.oi <= 0 or ref.price <= 0:
            return None
        return (last.oi - ref.oi) / ref.oi, (last.price - ref.price) / ref.price
