"""SMC modülünün PostgreSQL şeması. Tüm tablolar `smc_` önekli: mevcut botun tablolarıyla
(trades, bot_status, ...) çakışmaz ve aynı veritabanını paylaşır."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "smc_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class BotConfig(Base):
    __tablename__ = "smc_bot_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class BotState(Base):
    """Tek satır (id=1). Worker yazar, dashboard okur."""

    __tablename__ = "smc_bot_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(32), default="INITIALIZING")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_mode_reasons: Mapped[list] = mapped_column(JSON, default=list)
    manual_hold_reasons: Mapped[list] = mapped_column(JSON, default=list)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    worker_started_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    ws_market_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    ws_user_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    rest_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    day_key: Mapped[str | None] = mapped_column(String(10), nullable=True)
    day_start_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    month_start_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    month_key: Mapped[str | None] = mapped_column(String(7), nullable=True)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    available_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class BotCommand(Base):
    """Dashboard -> worker komut kuyruğu (KILL, RESUME). Dashboard worker'a doğrudan bağlı değildir."""

    __tablename__ = "smc_bot_commands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    requested_by: Mapped[str] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class TrialState(Base):
    __tablename__ = "smc_trial_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trial_started_at: Mapped[datetime] = mapped_column(TZ)
    trial_expires_at: Mapped[datetime] = mapped_column(TZ)
    expired_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class SymbolInfo(Base):
    __tablename__ = "smc_symbols"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    tick_size: Mapped[str] = mapped_column(String(32))
    step_size: Mapped[str] = mapped_column(String(32))
    min_qty: Mapped[str] = mapped_column(String(32))
    max_qty: Mapped[str] = mapped_column(String(32))
    market_step_size: Mapped[str] = mapped_column(String(32))
    market_max_qty: Mapped[str] = mapped_column(String(32))
    min_notional: Mapped[float] = mapped_column(Float)
    max_leverage: Mapped[int] = mapped_column(Integer, default=10)
    maint_margin_rate: Mapped[float] = mapped_column(Float, default=0.004)
    maint_amount: Mapped[float] = mapped_column(Float, default=0.0)
    in_universe: Mapped[bool] = mapped_column(Boolean, default=False)
    quote_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class MarketSnapshot(Base):
    __tablename__ = "smc_market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fear_greed: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Signal(Base):
    __tablename__ = "smc_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    candle_time: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[int] = mapped_column(Integer)
    max_score: Mapped[int] = mapped_column(Integer, default=15)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp3: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("symbol", "side", "candle_time", name="uq_smc_signal_candle"),)


class SignalComponent(Base):
    __tablename__ = "smc_signal_components"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("smc_signals.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(32))
    points: Mapped[int] = mapped_column(Integer)
    max_points: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Position(Base):
    __tablename__ = "smc_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("smc_signals.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(32), index=True)
    leverage: Mapped[int] = mapped_column(Integer)
    planned_entry: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_qty: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_qty: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_stop: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    tp1_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp3_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_plan: Mapped[list] = mapped_column(JSON, default=list)  # [{name, price, qty, filled}]
    risk_amount: Mapped[float] = mapped_column(Float)
    open_risk: Mapped[float] = mapped_column(Float, default=0.0)
    liq_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    be_active: Mapped[bool] = mapped_column(Boolean, default=False)
    tp_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    entry_filled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Order(Base):
    __tablename__ = "smc_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("smc_positions.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    client_id: Mapped[str] = mapped_column(String(40), unique=True)
    exchange_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_algo: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str] = mapped_column(String(16))  # ENTRY, SL, TP1, TP2, TP3, EXIT
    side: Mapped[str] = mapped_column(String(8))
    type: Mapped[str] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="NEW")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())


class Fill(Base):
    __tablename__ = "smc_fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("smc_positions.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange_trade_id: Mapped[str] = mapped_column(String(40))
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    commission_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    is_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(TZ)
    __table_args__ = (UniqueConstraint("symbol", "exchange_trade_id", name="uq_smc_fill_trade"),)


class Trade(Base):
    __tablename__ = "smc_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("smc_positions.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[datetime] = mapped_column(TZ)
    quantity: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    stop_price: Mapped[float] = mapped_column(Float)
    tp1_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp3_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_amount: Mapped[float] = mapped_column(Float)
    planned_rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float)
    funding: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime] = mapped_column(TZ)
    exit_reason: Mapped[str] = mapped_column(String(32))
    signal_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tp_hits: Mapped[list] = mapped_column(JSON, default=list)


class PnlSnapshot(Base):
    __tablename__ = "smc_pnl_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    equity: Mapped[float] = mapped_column(Float)
    available_balance: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    margin_used: Mapped[float] = mapped_column(Float)
    open_risk: Mapped[float] = mapped_column(Float)
    daily_pnl: Mapped[float] = mapped_column(Float)


class _EventMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskEvent(_EventMixin, Base):
    __tablename__ = "smc_risk_events"
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SystemEvent(_EventMixin, Base):
    __tablename__ = "smc_system_events"
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)


class TelegramEvent(_EventMixin, Base):
    __tablename__ = "smc_telegram_events"
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "smc_audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
