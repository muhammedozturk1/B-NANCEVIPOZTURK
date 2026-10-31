"""Korelasyon riski. BTC/ETH/SOL vb. yüksek korelasyonlu coinler bağımsız risk sayılmaz."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from backend.smc.config import Settings
from backend.smc.core.types import Side


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 20:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def correlation(closes_a: Sequence[float], closes_b: Sequence[float]) -> float | None:
    return pearson(log_returns(closes_a), log_returns(closes_b))


@dataclass
class CorrelationDecision:
    blocked: bool
    risk_factor: float
    correlated_with: list[tuple[str, float]]
    reason: str = ""


def evaluate_correlation(symbol: str, side: Side, open_positions: Sequence[tuple[str, Side]],
                         correlations: dict[str, float | None], cfg: Settings) -> CorrelationDecision:
    """
    - Aynı sembolde ikinci pozisyon yok (risk engine ayrıca kontrol eder).
    - Aynı yönde yüksek korelasyonlu pozisyon sayısı >= max_correlated_same_direction ise yeni pozisyon engellenir
      (varsayılan 2 → 3. korelasyonlu aynı yön pozisyon reddedilir).
    - Aynı yönde yüksek korelasyonlu pozisyon varsa efektif risk azaltılır.
    - Ters yöndeki yüksek korelasyonlu pozisyon hedge sayılır; risk azaltılmaz ama engellenmez.
    """
    same_dir = []
    for sym, s in open_positions:
        if s is not side:
            continue
        c = correlations.get(sym)
        if c is None:
            # bilinmiyorsa muhafazakâr davran: BTC exposure'ı varsay
            c = cfg.correlation_threshold
        if c >= cfg.correlation_threshold:
            same_dir.append((sym, c))
    if len(same_dir) >= cfg.max_correlated_same_direction:
        return CorrelationDecision(True, 0.0, same_dir, "too_many_correlated_same_direction")
    factor = cfg.correlated_risk_factor if same_dir else 1.0
    return CorrelationDecision(False, factor, same_dir)
