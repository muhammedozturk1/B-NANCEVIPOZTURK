"""Testler için bilinen SMC kurulumu içeren sentetik mumlar."""
from __future__ import annotations

import numpy as np

from backend.smcbot.smc import Candles

T0 = 1790812800000  # 2026-10-01 00:00 UTC
M15 = 900_000
H1 = 3_600_000


def _path(points: list[tuple[int, float]]) -> list[float]:
    out: list[float] = []
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        for i in range(i0, i1):
            out.append(p0 + (p1 - p0) * (i - i0) / (i1 - i0))
    out.append(points[-1][1])
    return out


def long_case() -> tuple[Candles, Candles]:
    closes = _path([(0, 95), (80, 107), (110, 101), (120, 100.0), (130, 102), (140, 100.02),
                    (150, 103), (167, 100.6)])
    n = len(closes)  # 168 bar (0..167)
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) + 0.1 for a, b in zip(o, closes)]
    lo = [min(a, b) - 0.1 for a, b in zip(o, closes)]
    c = list(closes)
    # 168: av mumu (eşit diplerin altına iğne, içeride kapanış)
    o.append(100.6); h.append(100.7); lo.append(99.6); c.append(100.4)
    # 169-171: displacement + FVG + MSS (103.1 üstü kapanış)
    o.append(100.4); h.append(101.0); lo.append(100.3); c.append(100.9)
    o.append(100.9); h.append(102.7); lo.append(100.85); c.append(102.6)
    o.append(102.6); h.append(103.6); lo.append(102.4); c.append(103.5)
    n = len(c)
    vol = np.full(n, 1000.0)
    tb = np.full(n, 500.0)
    tb[168] = 650.0          # av mumunda alıcı agresif (emilim)
    tb[169:172] = 700.0      # kırılımda alım baskın
    ltf = Candles(np.arange(n, dtype=np.int64) * M15 + T0, np.array(o), np.array(h), np.array(lo), np.array(c),
                  vol, tb, M15)

    # HTF: ltf ile aynı anda biten, düzenli yükselen zigzag (trend yukarı)
    hn = 120
    end = T0 + n * M15
    ht = end - (hn - np.arange(hn)) * H1
    base = []
    p = 70.0
    for i in range(hn):
        p += 0.6 if (i % 8) < 5 else -0.5
        base.append(p)
    hc = np.array(base)
    ho = np.concatenate([[hc[0]], hc[:-1]])
    hh = np.maximum(ho, hc) + 0.2
    hl = np.minimum(ho, hc) - 0.2
    htf = Candles(ht.astype(np.int64), ho, hh, hl, hc, np.full(hn, 1.0), np.full(hn, 0.5), H1)
    return ltf, htf


def mirror_case(ltf: Candles, htf: Candles, pivot: float = 200.0) -> tuple[Candles, Candles]:
    """Fiyatları ters çevirir: long kurulum short kurulum olur."""
    def m(c: Candles) -> Candles:
        return Candles(c.time, pivot - c.open, pivot - c.low, pivot - c.high, pivot - c.close,
                       c.volume, c.volume - c.taker_buy, c.interval_ms)
    return m(ltf), m(htf)
