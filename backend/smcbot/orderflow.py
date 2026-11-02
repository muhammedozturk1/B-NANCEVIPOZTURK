"""Order flow ve likidite verileri.

CoinGlass anahtarı olmadan da çalışan ücretsiz kaynaklar:
- CVD: Binance mumlarındaki agresif alıcı hacminden (taker buy) hesaplanır.
- Likidasyonlar: Binance'in tüm piyasa likidasyon akışı (!forceOrder@arr).
- Tahmini likidasyon haritası: yüksek hacimli mumlarda açılan 25x/50x/100x
  pozisyonların likidasyon fiyatları kümelenir (CoinGlass ısı haritasının yaklaşığı).
- Open interest değişimi, funding oranı, emir defteri dengesizliği.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace

import numpy as np

from .smc import Candles

LEVERAGE_MIX = ((25, 0.45), (50, 0.35), (100, 0.20))
MMR = 0.004


def est_liq_clusters(c: Candles, t: int, lookback: int = 200, bin_pct: float = 0.25,
                     top: int = 6) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """t anındaki tahmini likidasyon kümeleri.

    Dönüş: (fiyatın üstündekiler, altındakiler). Üsttekiler = short likidasyonları
    (long için hedef / mıknatıs), alttakiler = long likidasyonları.
    Seri aynalanmışsa yönler de otomatik ters döner.
    """
    lo = max(0, t - lookback)
    if t - lo < 20:
        return [], []
    qv = c.volume[lo:t + 1] * np.abs(c.close[lo:t + 1])
    thresh = np.quantile(qv, 0.7)
    price = c.close[t]
    bins: dict[int, float] = defaultdict(float)
    width = abs(price) * bin_pct / 100
    if width <= 0:
        return [], []
    for k in np.nonzero(qv >= thresh)[0]:
        i = lo + int(k)
        p0 = c.close[i]
        after_hi = c.high[i + 1:t + 1].max() if i < t else -np.inf
        after_lo = c.low[i + 1:t + 1].min() if i < t else np.inf
        for lev, w in LEVERAGE_MIX:
            d = abs(p0) * (1 / lev - MMR)
            short_liq = p0 + d       # bu seviyeye gelirse short'lar likide olur
            long_liq = p0 - d
            # Kovalar güncel fiyata göre sabitlenir (aynalamada simetrik kalır)
            if after_hi < short_liq:
                bins[int(np.floor((short_liq - price) / width))] += qv[k] * w
            if after_lo > long_liq:
                bins[int(np.floor((long_liq - price) / width))] += qv[k] * w
    center = lambda b: price + (b + 0.5) * width  # noqa: E731
    above = [(center(b), v) for b, v in bins.items() if b >= 0]
    below = [(center(b), v) for b, v in bins.items() if b < 0]
    above = sorted(sorted(above, key=lambda x: -x[1])[:top])
    below = sorted(sorted(below, key=lambda x: -x[1])[:top], reverse=True)
    return [(float(p), float(v)) for p, v in above], [(float(p), float(v)) for p, v in below]


@dataclass
class FlowSnapshot:
    """Canlı akış verileri (long bakış açısıyla yorumlanır; short için mirrored())."""
    liq_against_usd: float = 0.0     # av yönünde likide olanlar (long kurulumda: long likidasyonları)
    liq_baseline_usd: float = 0.0    # aynı süre için 24 saatlik ortalama
    oi_change_pct: float | None = None
    funding: float | None = None     # oran (0.0001 = %0.01)
    ob_imbalance: float | None = None  # +: alış ağırlıklı
    cg_clusters_above: list = field(default_factory=list)  # CoinGlass (varsa)
    cg_clusters_below: list = field(default_factory=list)
    liq_for_usd: float = 0.0         # karşı taraf likidasyonları

    def mirrored(self) -> "FlowSnapshot":
        return replace(
            self,
            liq_against_usd=self.liq_for_usd, liq_for_usd=self.liq_against_usd,
            funding=None if self.funding is None else -self.funding,
            ob_imbalance=None if self.ob_imbalance is None else -self.ob_imbalance,
            cg_clusters_above=[(-p, v) for p, v in self.cg_clusters_below],
            cg_clusters_below=[(-p, v) for p, v in self.cg_clusters_above],
        )


class LiquidationTracker:
    """!forceOrder@arr akışından coin başına likidasyonları tutar (24 saat)."""

    def __init__(self, now=None) -> None:
        self.events: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self.now = now or (lambda: time.time() * 1000)

    def add(self, data: dict) -> None:
        o = data.get("o", {}) if isinstance(data, dict) else {}
        sym = o.get("s")
        if not sym:
            return
        try:
            usd = float(o.get("ap") or o.get("p") or 0) * float(o.get("z") or o.get("q") or 0)
        except (TypeError, ValueError):
            return
        # side SELL = long pozisyon likide edildi, BUY = short likide edildi
        self.events[sym].append((int(o.get("T") or self.now()), o.get("S"), usd))

    def window(self, symbol: str, minutes: int) -> tuple[float, float]:
        cutoff = self.now() - minutes * 60_000
        longs = shorts = 0.0
        for ts, side, usd in self.events.get(symbol, ()):
            if ts >= cutoff:
                if side == "SELL":
                    longs += usd
                else:
                    shorts += usd
        return longs, shorts

    def baseline(self, symbol: str, minutes: int) -> tuple[float, float]:
        """Son 24 saatin aynı uzunluktaki pencere ortalaması."""
        cutoff = self.now() - 24 * 3_600_000
        longs = shorts = 0.0
        for ts, side, usd in self.events.get(symbol, ()):
            if ts >= cutoff:
                if side == "SELL":
                    longs += usd
                else:
                    shorts += usd
        k = (24 * 60) / minutes
        return longs / k, shorts / k

    def total_24h(self) -> float:
        cutoff = self.now() - 24 * 3_600_000
        return sum(usd for dq in self.events.values() for ts, _, usd in dq if ts >= cutoff)


def orderbook_imbalance(depth: dict, mid: float, pct: float = 1.0) -> float | None:
    try:
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        bids = sum(float(p) * float(q) for p, q in depth.get("bids", []) if float(p) >= lo)
        asks = sum(float(p) * float(q) for p, q in depth.get("asks", []) if float(p) <= hi)
        tot = bids + asks
        return (bids - asks) / tot if tot > 0 else None
    except (TypeError, ValueError):
        return None


def oi_change_pct(hist: list[dict], periods: int = 6) -> float | None:
    try:
        vals = [float(h["sumOpenInterestValue"]) for h in hist]
        if len(vals) < periods + 1 or vals[-periods - 1] <= 0:
            return None
        window = vals[-periods - 1:]
        peak = max(window[:-1])
        return (window[-1] - peak) / peak * 100
    except (KeyError, TypeError, ValueError):
        return None
