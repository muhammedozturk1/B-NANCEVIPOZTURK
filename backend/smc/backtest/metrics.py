from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence


@dataclass
class BTTrade:
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    risk_amount: float
    gross_pnl: float
    commission: float
    funding: float
    slippage: float
    net_pnl: float
    r: float
    exit_reason: str
    tp_hits: list[str]
    score: int
    leverage: int
    partial_fill: bool

    def as_dict(self) -> dict:
        return asdict(self)


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(max DD tutarı, max DD yüzdesi)."""
    peak, dd_abs, dd_pct = float("-inf"), 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd_abs = max(dd_abs, peak - e)
            dd_pct = max(dd_pct, (peak - e) / peak)
    return dd_abs, dd_pct


def compute_metrics(trades: Sequence[BTTrade], equity_curve: Sequence[float], starting_equity: float) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    streak = max_streak = 0
    for t in trades:
        streak = streak + 1 if t.net_pnl < 0 else 0
        max_streak = max(max_streak, streak)
    dd_abs, dd_pct = max_drawdown([starting_equity, *equity_curve])
    hit = lambda name: sum(1 for t in trades if name in t.tp_hits) / n if n else 0.0  # noqa: E731
    return {
        "total_trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "net_pnl": sum(t.net_pnl for t in trades),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "average_r": sum(t.r for t in trades) / n if n else 0.0,
        "max_drawdown": dd_abs,
        "max_drawdown_pct": dd_pct,
        "max_consecutive_loss": max_streak,
        "average_trade_duration_min": (sum(t.exit_time - t.entry_time for t in trades) / n / 60_000) if n else 0.0,
        "tp1_hit_rate": hit("TP1"),
        "tp2_hit_rate": hit("TP2"),
        "tp3_hit_rate": hit("TP3"),
        "sl_rate": sum(1 for t in trades if t.exit_reason == "SL") / n if n else 0.0,
        "total_commission": sum(t.commission for t in trades),
        "total_funding": sum(t.funding for t in trades),
        "final_equity": equity_curve[-1] if equity_curve else starting_equity,
        "partial_fills": sum(1 for t in trades if t.partial_fill),
    }
