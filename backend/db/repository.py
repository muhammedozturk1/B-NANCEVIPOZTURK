"""
Trade ve EngineState tabloları için tekrar kullanılabilir veritabanı işlemleri.
Böylece engine kodları SQLAlchemy detaylarıyla uğraşmaz, sade fonksiyon çağırır.
"""
from datetime import datetime, timedelta
from backend import config
from backend.db.database import get_session
from backend.db.models import Trade, EngineState, TradeStatus, BotCapital


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


def has_open_position_any_engine(symbol: str) -> bool:
    """
    KRİTİK GÜVENLİK KONTROLÜ: Herhangi bir motorun (scalp/day/swing fark etmez)
    bu sembolde açık pozisyonu var mı? Binance'te pozisyonlar sembol bazında
    NETLEŞTİĞİ için, 3 motor birbirinden habersiz aynı sembole girerse bunlar
    borsada tek, çok daha büyük bir pozisyona dönüşür - marj tavanı her motor
    için ayrı ayrı doğru hesaplansa bile toplamda kontrolsüz büyür. Bu yüzden
    yeni işlem açmadan önce TÜM motorlar için kontrol edilir.
    """
    session = get_session()
    try:
        return session.query(Trade).filter(
            Trade.symbol == symbol, Trade.status == TradeStatus.OPEN
        ).first() is not None
    finally:
        session.close()


def is_symbol_in_cooldown(symbol: str) -> bool:
    """Bu sembolde son SYMBOL_COOLDOWN_MINUTES içinde kapanmış bir işlem var mı?
    Varsa hızlı ardışık aç/kapa döngülerini önlemek için yeni işlem engellenir."""
    session = get_session()
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=config.SYMBOL_COOLDOWN_MINUTES)
        recent = session.query(Trade).filter(
            Trade.symbol == symbol,
            Trade.status != TradeStatus.OPEN,
            Trade.closed_at.isnot(None),
            Trade.closed_at >= cutoff,
        ).first()
        return recent is not None
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


# ------------------------------------------------------------------
# SANAL KASA (VIRTUAL BALANCE)
# ------------------------------------------------------------------
def get_virtual_balance() -> float:
    """Botun risk hesaplamalarında kullandığı sanal kasayı döner (yoksa STARTING_BALANCE ile oluşturur)."""
    session = get_session()
    try:
        capital = session.query(BotCapital).first()
        if not capital:
            capital = BotCapital(balance=config.STARTING_BALANCE)
            session.add(capital)
            session.commit()
            session.refresh(capital)
        return capital.balance
    finally:
        session.close()


def update_virtual_balance(pnl_usd: float) -> float:
    """Bir işlem kapandığında sanal kasaya kâr/zararı işler, yeni bakiyeyi döner."""
    session = get_session()
    try:
        capital = session.query(BotCapital).first()
        if not capital:
            capital = BotCapital(balance=config.STARTING_BALANCE)
            session.add(capital)
        capital.balance += pnl_usd
        session.commit()
        session.refresh(capital)
        return capital.balance
    finally:
        session.close()


# ------------------------------------------------------------------
# TELEGRAM MESAJ ID (reply-thread güncellemeleri için)
# ------------------------------------------------------------------
def set_telegram_message_id(trade_id: int, message_id: int):
    session = get_session()
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            trade.telegram_message_id = message_id
            session.commit()
    finally:
        session.close()


def get_trades_since(since_datetime) -> list:
    """Haftalık özet raporu için, belirli bir tarihten sonra AÇILAN tüm işlemleri döner."""
    session = get_session()
    try:
        return session.query(Trade).filter(Trade.opened_at >= since_datetime).all()
    finally:
        session.close()
