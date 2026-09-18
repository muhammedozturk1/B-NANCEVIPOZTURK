"""
KILL-SWITCH Modülü.

İki katmanlı güvenlik:
  1) GENEL: Kasa günlük olarak %30 zarar ederse TÜM bot durur (yeni işlem açılmaz).
  2) MOTOR BAZLI: Bir motor art arda N kayıp yaparsa SADECE o motor geçici durur,
     diğerleri çalışmaya devam eder.
"""
import logging
from datetime import datetime, timedelta
from backend import config
from backend.db.database import get_session
from backend.db.models import DailyPnL, EngineState

logger = logging.getLogger("kill_switch")


def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def get_or_create_daily_pnl(starting_balance: float) -> DailyPnL:
    session = get_session()
    try:
        today = _today_str()
        record = session.query(DailyPnL).filter(DailyPnL.date == today).first()
        if not record:
            record = DailyPnL(date=today, starting_balance=starting_balance, realized_pnl=0)
            session.add(record)
            session.commit()
            session.refresh(record)
        return record
    finally:
        session.close()


def record_realized_pnl(pnl_usd: float, current_balance_before_trade: float):
    """Bir işlem kapandığında günlük gerçekleşen PnL'e ekler ve kill-switch'i kontrol eder."""
    session = get_session()
    try:
        today = _today_str()
        record = session.query(DailyPnL).filter(DailyPnL.date == today).first()
        if not record:
            record = DailyPnL(date=today, starting_balance=current_balance_before_trade, realized_pnl=0)
            session.add(record)

        record.realized_pnl += pnl_usd

        loss_percent = (-record.realized_pnl / record.starting_balance) if record.starting_balance > 0 else 0
        if loss_percent >= config.DAILY_MAX_LOSS_PERCENT and not record.kill_switch_triggered:
            record.kill_switch_triggered = True
            logger.warning(f"🛑 GENEL KILL-SWITCH TETİKLENDİ! Günlük zarar: %{loss_percent*100:.1f}")

        session.commit()
    finally:
        session.close()


def is_global_kill_switch_active() -> bool:
    session = get_session()
    try:
        today = _today_str()
        record = session.query(DailyPnL).filter(DailyPnL.date == today).first()
        return bool(record and record.kill_switch_triggered)
    finally:
        session.close()


# ------------------------------------------------------------------
# MOTOR BAZLI ART ARDA KAYIP TAKİBİ
# ------------------------------------------------------------------
def record_engine_trade_result(engine: str, is_win: bool):
    """Bir işlem kapandığında (kâr/zarar) motorun art arda kayıp sayacını günceller."""
    session = get_session()
    try:
        state = session.query(EngineState).filter(EngineState.engine == engine).first()
        if not state:
            state = EngineState(engine=engine, is_active=True, consecutive_losses=0)
            session.add(state)

        if is_win:
            state.consecutive_losses = 0
        else:
            state.consecutive_losses += 1
            if state.consecutive_losses >= config.ENGINE_CONSECUTIVE_LOSS_LIMIT:
                state.is_active = False
                state.paused_until = datetime.utcnow() + timedelta(minutes=config.ENGINE_PAUSE_DURATION_MINUTES)
                logger.warning(f"⏸️ [{engine}] art arda {state.consecutive_losses} kayıp - motor "
                                f"{config.ENGINE_PAUSE_DURATION_MINUTES} dakika duraklatıldı.")

        session.commit()
    finally:
        session.close()


def reactivate_engine_if_pause_expired(engine: str):
    """Duraklama süresi dolan motoru tekrar aktif eder (yeni döngüde çağrılır)."""
    session = get_session()
    try:
        state = session.query(EngineState).filter(EngineState.engine == engine).first()
        if state and not state.is_active and state.paused_until and datetime.utcnow() >= state.paused_until:
            state.is_active = True
            state.consecutive_losses = 0
            state.paused_until = None
            session.commit()
            logger.info(f"▶️ [{engine}] duraklama süresi doldu, motor tekrar aktif.")
    finally:
        session.close()
