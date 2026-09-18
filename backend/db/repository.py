"""
Trade ve EngineState tabloları için tekrar kullanılabilir veritabanı işlemleri.
Böylece engine kodları SQLAlchemy detaylarıyla uğraşmaz, sade fonksiyon çağırır.
"""
from datetime import datetime
from backend.db.database import get_session
from backend.db.models import Trade, EngineState, TradeStatus


def count_open_positions(engine: str) -> int:
    session = get_session()
    try:
        return session.query(Trade).filter(
            Trade.engine == engine, Trade.status == TradeStatus.OPEN
        ).count()
    finally:
        session.close()


def has_open_position(engine: str, symbol: str) -> bool:
    session = get_session()
    try:
        return session.query(Trade).filter(
            Trade.engine == engine, Trade.symbol == symbol, Trade.status == TradeStatus.OPEN
        ).first() is not None
    finally:
        session.close()


def save_trade(**kwargs) -> Trade:
    session = get_session()
    try:
        trade = Trade(**kwargs)
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade
    finally:
        session.close()


def get_open_trades(engine: str = None):
    session = get_session()
    try:
        query = session.query(Trade).filter(Trade.status == TradeStatus.OPEN)
        if engine:
            query = query.filter(Trade.engine == engine)
        return query.all()
    finally:
        session.close()


def close_trade(trade_id: int, exit_price: float, pnl_usd: float, status: TradeStatus):
    session = get_session()
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            trade.exit_price = exit_price
            trade.pnl_usd = pnl_usd
            trade.status = status
            trade.closed_at = datetime.utcnow()
            session.commit()
        return trade
    finally:
        session.close()


def get_or_create_engine_state(engine: str) -> EngineState:
    session = get_session()
    try:
        state = session.query(EngineState).filter(EngineState.engine == engine).first()
        if not state:
            state = EngineState(engine=engine, is_active=True, consecutive_losses=0)
            session.add(state)
            session.commit()
            session.refresh(state)
        return state
    finally:
        session.close()


def is_engine_active(engine: str) -> bool:
    state = get_or_create_engine_state(engine)
    if not state.is_active and state.paused_until:
        if datetime.utcnow() >= state.paused_until:
            return True  # duraklama süresi doldu, tekrar aktif sayılabilir
        return False
    return state.is_active
