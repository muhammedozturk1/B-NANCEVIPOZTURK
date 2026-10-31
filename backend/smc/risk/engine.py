"""Risk Engine — Strategy Engine'den daha yüksek önceliğe sahiptir.

Strateji ne kadar güçlü olursa olsun; toplam açık risk, günlük zarar, kaldıraç,
liquidation buffer, likidite, spread veya SL güvenliği kurallarından biri ihlal
ediliyorsa işlem açılmaz. Fonksiyonlar saftır (I/O yok): live ve backtest aynı kodu kullanır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.smc.config import Settings
from backend.smc.core.types import Side, ensure_utc
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.correlation import CorrelationDecision, evaluate_correlation
from backend.smc.risk.plan import TPLevel, plan_take_profits, planned_rr
from backend.smc.risk.sizing import SizingResult, compute_position_size, round_stop_price
from backend.smc.strategy.signal import TradeSetup


@dataclass
class OpenPositionView:
    symbol: str
    side: Side
    open_risk: float


@dataclass
class PortfolioState:
    equity: float  # wallet + unrealized (commission/funding cüzdana yansımış haliyle)
    available_balance: float
    day_start_equity: float
    open_positions: list[OpenPositionView]
    consecutive_losses: int
    cooldown_until: datetime | None
    now: datetime

    @property
    def total_open_risk(self) -> float:
        return sum(p.open_risk for p in self.open_positions)


@dataclass
class TradingGate:
    """Bot seviyesindeki kapılar (trial, kill switch, safe mode, reconciliation, websocket)."""

    can_trade: bool
    reason: str = ""


@dataclass
class Candidate:
    setup: TradeSetup
    spread_pct: float | None
    depth_usdt: float | None
    correlations: dict[str, float | None] = field(default_factory=dict)


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    sizing: SizingResult | None = None
    stop: float | None = None
    tp_levels: list[TPLevel] = field(default_factory=list)
    planned_rr: float = 0.0
    risk_factor: float = 1.0
    correlation: CorrelationDecision | None = None

    def reject(self, reason: str) -> "RiskDecision":
        self.allowed = False
        self.reasons.append(reason)
        return self


def daily_loss(portfolio: PortfolioState) -> float:
    """Gün başı equity'ye göre zarar (realized + unrealized + commission + funding; hepsi equity'de)."""
    return max(0.0, portfolio.day_start_equity - portfolio.equity)


def daily_loss_breached(portfolio: PortfolioState, cfg: Settings) -> bool:
    return daily_loss(portfolio) >= cfg.daily_loss_limit * portfolio.day_start_equity - 1e-9


def cooldown_active(portfolio: PortfolioState) -> bool:
    cu = ensure_utc(portfolio.cooldown_until)
    return cu is not None and portfolio.now < cu


def register_trade_result(consecutive_losses: int, net_pnl: float, exit_reason: str, now: datetime,
                          cfg: Settings) -> tuple[int, datetime | None]:
    """Ardışık SL sayacı. 3 ardışık zararlı stop-loss → 30 dk cooldown, sayaç sıfırlanır."""
    if exit_reason == "SL" and net_pnl < 0:
        consecutive_losses += 1
        if consecutive_losses >= cfg.max_consecutive_sl:
            return 0, now + timedelta(minutes=cfg.cooldown_minutes)
        return consecutive_losses, None
    if net_pnl > 0:
        return 0, None
    return consecutive_losses, None


def evaluate_trade(candidate: Candidate, portfolio: PortfolioState, gate: TradingGate, filters: SymbolFilters,
                   cfg: Settings) -> RiskDecision:
    d = RiskDecision(allowed=True)
    setup = candidate.setup

    if not gate.can_trade:
        return d.reject(f"gate:{gate.reason}")
    if daily_loss_breached(portfolio, cfg):
        return d.reject("daily_loss_limit")
    if cooldown_active(portfolio):
        return d.reject("cooldown_active")
    if len(portfolio.open_positions) >= cfg.max_open_positions:
        return d.reject("max_open_positions")
    if any(p.symbol == setup.symbol for p in portfolio.open_positions):
        return d.reject("position_exists_on_symbol")
    if candidate.spread_pct is None or candidate.spread_pct > cfg.max_spread_pct:
        return d.reject(f"spread:{candidate.spread_pct}")
    if candidate.depth_usdt is None or candidate.depth_usdt < cfg.min_book_depth_usdt:
        return d.reject(f"liquidity:{candidate.depth_usdt}")

    corr = evaluate_correlation(setup.symbol, setup.side, [(p.symbol, p.side) for p in portfolio.open_positions],
                                candidate.correlations, cfg)
    d.correlation = corr
    if corr.blocked:
        return d.reject(corr.reason)
    factor = corr.risk_factor * (cfg.correlated_risk_factor if setup.risk_reduce else 1.0)
    d.risk_factor = factor

    stop = round_stop_price(setup.stop, setup.side, filters.tick_size)
    d.stop = stop
    sizing = compute_position_size(side=setup.side, equity=portfolio.equity,
                                   available_balance=portfolio.available_balance, entry=setup.entry, stop=stop,
                                   filters=filters, cfg=cfg, risk_factor=factor)
    d.sizing = sizing
    if not sizing.ok:
        return d.reject(f"sizing:{sizing.reason}")

    max_risk = cfg.max_total_open_risk * portfolio.equity
    if portfolio.total_open_risk + sizing.risk_actual > max_risk + 1e-9:
        return d.reject(f"total_open_risk:{portfolio.total_open_risk + sizing.risk_actual:.4f}>{max_risk:.4f}")

    levels = plan_take_profits(setup.side, setup.entry, stop, sizing.qty, filters, cfg)
    d.tp_levels = levels
    if not levels or any(l.qty <= 0 for l in levels):
        return d.reject("invalid_tp")
    rr = planned_rr(setup.side, setup.entry, stop, levels, cfg)
    d.planned_rr = rr
    if rr < cfg.min_rr:
        return d.reject(f"rr_below_min:{rr:.2f}")
    return d
