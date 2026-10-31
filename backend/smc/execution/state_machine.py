"""Pozisyon / emir state machine ve bot durumları."""
from __future__ import annotations

from enum import Enum


class PosState(str, Enum):
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    RISK_CHECK = "RISK_CHECK"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    ENTRY_FILLED = "ENTRY_FILLED"
    SL_PLACED = "SL_PLACED"
    TP_ORDERS_PLACED = "TP_ORDERS_PLACED"
    ACTIVE = "ACTIVE"
    TP1_FILLED = "TP1_FILLED"
    BREAKEVEN_ACTIVE = "BREAKEVEN_ACTIVE"
    TP2_FILLED = "TP2_FILLED"
    TP3_FILLED = "TP3_FILLED"
    CLOSED = "CLOSED"
    # hata durumları
    ORDER_REJECTED = "ORDER_REJECTED"
    SL_PLACEMENT_FAILED = "SL_PLACEMENT_FAILED"
    TP_PLACEMENT_FAILED = "TP_PLACEMENT_FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    SAFE_MODE = "SAFE_MODE"


S = PosState
TERMINAL = {S.CLOSED, S.ORDER_REJECTED}
# Borsada açık pozisyonu olan (yönetilmesi gereken) durumlar
LIVE_STATES = {S.PARTIALLY_FILLED, S.ENTRY_FILLED, S.SL_PLACED, S.TP_ORDERS_PLACED, S.ACTIVE, S.TP1_FILLED,
               S.BREAKEVEN_ACTIVE, S.TP2_FILLED, S.TP3_FILLED, S.SL_PLACEMENT_FAILED, S.TP_PLACEMENT_FAILED,
               S.RECONCILIATION_REQUIRED, S.EMERGENCY_EXIT, S.SAFE_MODE}
OPEN_STATES = LIVE_STATES | {S.SIGNAL_DETECTED, S.RISK_CHECK, S.ORDER_SUBMITTED}

_T: dict[PosState, set[PosState]] = {
    S.SIGNAL_DETECTED: {S.RISK_CHECK, S.ORDER_REJECTED},
    S.RISK_CHECK: {S.ORDER_SUBMITTED, S.ORDER_REJECTED},
    S.ORDER_SUBMITTED: {S.PARTIALLY_FILLED, S.ENTRY_FILLED, S.ORDER_REJECTED, S.CLOSED},
    S.PARTIALLY_FILLED: {S.ENTRY_FILLED},
    S.ENTRY_FILLED: {S.SL_PLACED, S.SL_PLACEMENT_FAILED},
    S.SL_PLACED: {S.TP_ORDERS_PLACED, S.TP_PLACEMENT_FAILED},
    S.TP_ORDERS_PLACED: {S.ACTIVE},
    S.TP_PLACEMENT_FAILED: {S.ACTIVE, S.TP_ORDERS_PLACED},
    S.ACTIVE: {S.TP1_FILLED, S.TP2_FILLED, S.TP3_FILLED},
    S.TP1_FILLED: {S.BREAKEVEN_ACTIVE, S.TP2_FILLED, S.TP3_FILLED},
    S.BREAKEVEN_ACTIVE: {S.TP2_FILLED, S.TP3_FILLED},
    S.TP2_FILLED: {S.TP3_FILLED},
    S.TP3_FILLED: {S.CLOSED},
    S.SL_PLACEMENT_FAILED: {S.EMERGENCY_EXIT},
    S.EMERGENCY_EXIT: {S.CLOSED},
    S.RECONCILIATION_REQUIRED: {S.ACTIVE, S.BREAKEVEN_ACTIVE, S.TP1_FILLED, S.TP2_FILLED, S.EMERGENCY_EXIT},
    S.SAFE_MODE: {S.ACTIVE, S.EMERGENCY_EXIT},
}
# Pozisyon açıkken her an gerçekleşebilecek geçişler (SL tetiklenmesi, acil çıkış, reconciliation)
_ANYTIME = {S.CLOSED, S.EMERGENCY_EXIT, S.RECONCILIATION_REQUIRED, S.SAFE_MODE}


class InvalidTransition(Exception):
    pass


def can_transition(src: PosState | str, dst: PosState | str) -> bool:
    src, dst = PosState(src), PosState(dst)
    if src in TERMINAL:
        return False
    if src == dst:
        return True
    if dst in _ANYTIME and src in LIVE_STATES:
        return True
    return dst in _T.get(src, set())


def assert_transition(src: PosState | str, dst: PosState | str) -> PosState:
    if not can_transition(src, dst):
        raise InvalidTransition(f"{src} -> {dst}")
    return PosState(dst)


class BotStatus(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    STOP_NEW_TRADES = "STOP_NEW_TRADES"  # günlük zarar limiti
    SAFE_MODE = "SAFE_MODE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    EXPIRED = "EXPIRED"


# Manuel müdahale gerektiren (otomatik temizlenmeyen) durumlar
STICKY_STATUSES = {BotStatus.EXPIRED, BotStatus.EMERGENCY_STOP}
