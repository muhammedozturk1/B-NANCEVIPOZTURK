"""
Veritabanı şeması.

Trade tablosu dashboard'daki tüm mum-üstü işaretlemeler (giriş/TP/SL noktaları)
ve motor bazlı istatistikler için tek kaynak.
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Enum
from sqlalchemy.sql import func
from backend.db.database import Base
import enum


class TradeStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED_TP1 = "closed_tp1"
    CLOSED_TP2 = "closed_tp2"
    CLOSED_SL = "closed_sl"
    CLOSED_BE = "closed_be"          # breakeven'da kapandı
    CLOSED_MANUAL = "closed_manual"  # momentum kaybı / ters dönüş nedeniyle erken kapama


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    engine = Column(String, nullable=False)          # scalp / day / swing
    symbol = Column(String, nullable=False)           # BTC/USDT
    side = Column(String, nullable=False)              # buy / sell
    leverage = Column(Integer, nullable=False)

    entry_price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)             # pozisyon miktarı
    risk_usd = Column(Float, nullable=False)           # işlem başına risklenen $ (min 5$)

    stop_loss = Column(Float, nullable=False)
    take_profit_1 = Column(Float, nullable=False)
    take_profit_2 = Column(Float, nullable=False)

    moved_to_breakeven = Column(Boolean, default=False)

    status = Column(Enum(TradeStatus), default=TradeStatus.OPEN)
    exit_price = Column(Float, nullable=True)
    pnl_usd = Column(Float, nullable=True)

    confluence_score = Column(Integer, nullable=False)     # kaç teknik onay verdi
    strategies_used = Column(String, nullable=False)        # "ema,vwap,smc" gibi virgüllü liste

    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)


class EngineState(Base):
    """Her motorun anlık durumu: aktif mi, kaç kayıp üst üste geldi vb."""
    __tablename__ = "engine_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    engine = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    consecutive_losses = Column(Integer, default=0)
    paused_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())


class DailyPnL(Base):
    """Gün sonu / kill-switch takibi için günlük kâr-zarar özeti."""
    __tablename__ = "daily_pnl"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, unique=True, nullable=False)  # "2026-09-17"
    starting_balance = Column(Float, nullable=False)
    realized_pnl = Column(Float, default=0)
    kill_switch_triggered = Column(Boolean, default=False)
