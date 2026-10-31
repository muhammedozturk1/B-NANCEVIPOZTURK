"""72 saatlik trial. Başlangıç/bitiş PostgreSQL'e yazılır; Render restart'ı süreyi sıfırlamaz."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.smc.core.types import ensure_utc
from backend.smc.database.models import TrialState


@dataclass
class TrialStatus:
    started_at: datetime
    expires_at: datetime
    expired: bool
    remaining_s: float


def ensure_trial(session: Session, now: datetime, hours: int) -> TrialState:
    row = session.get(TrialState, 1)
    if row is None:
        row = TrialState(id=1, trial_started_at=now, trial_expires_at=now + timedelta(hours=hours))
        session.add(row)
        session.flush()
    return row


def trial_status(row: TrialState, now: datetime) -> TrialStatus:
    exp = ensure_utc(row.trial_expires_at)
    remaining = (exp - now).total_seconds()
    return TrialStatus(ensure_utc(row.trial_started_at), exp, remaining <= 0, max(0.0, remaining))
