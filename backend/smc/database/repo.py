"""Veritabanı erişim katmanı. Her metod kendi kısa transaction'ını açar."""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from backend.smc.core.types import utc_now
from backend.smc.database.db import Database
from backend.smc.database.models import (AuditLog, BotCommand, BotConfig, BotState, Fill, Order, PnlSnapshot, Position,
                                 RiskEvent, Signal, SignalComponent, SymbolInfo, SystemEvent, TelegramEvent, Trade)
from backend.smc.execution.state_machine import OPEN_STATES, PosState, assert_transition
from backend.smc.market_data.universe import SymbolFilters

log = logging.getLogger(__name__)


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ bot state
    def state(self) -> BotState:
        with self.db.session() as s:
            row = s.get(BotState, 1)
            if row is None:
                row = BotState(id=1, state="INITIALIZING", safe_mode_reasons=[], manual_hold_reasons=[])
                s.add(row)
                s.flush()
            return row

    def update_state(self, **kw: Any) -> BotState:
        with self.db.session() as s:
            row = s.get(BotState, 1)
            if row is None:
                row = BotState(id=1, safe_mode_reasons=[], manual_hold_reasons=[])
                s.add(row)
            for k, v in kw.items():
                setattr(row, k, v)
            s.flush()
            return row

    def save_config(self, key: str, value: dict) -> None:
        with self.db.session() as s:
            row = s.scalar(select(BotConfig).where(BotConfig.key == key))
            if row is None:
                s.add(BotConfig(key=key, value=value))
            else:
                row.value = value

    # --------------------------------------------------------------- events
    def system(self, level: str, kind: str, message: str, **details: Any) -> None:
        getattr(log, level.lower(), log.info)("%s: %s %s", kind, message, details or "")
        try:
            with self.db.session() as s:
                s.add(SystemEvent(level=level, kind=kind, message=message, details=_jsonable(details)))
        except Exception:  # DB yoksa logla, çökme
            log.exception("system event yazılamadı")

    def risk_event(self, kind: str, symbol: str | None = None, **details: Any) -> None:
        with self.db.session() as s:
            s.add(RiskEvent(kind=kind, symbol=symbol, details=_jsonable(details)))

    def audit(self, actor: str, action: str, **details: Any) -> None:
        with self.db.session() as s:
            s.add(AuditLog(actor=actor, action=action, details=_jsonable(details)))

    def telegram_log(self, kind: str, text: str, sent: bool, error: str | None = None) -> None:
        with self.db.session() as s:
            s.add(TelegramEvent(kind=kind, text=text, sent=sent, error=error, details={}))

    # -------------------------------------------------------------- commands
    def add_command(self, command: str, requested_by: str) -> int:
        with self.db.session() as s:
            c = BotCommand(command=command, requested_by=requested_by, status="PENDING")
            s.add(c)
            s.flush()
            return c.id

    def pending_commands(self) -> list[BotCommand]:
        with self.db.session() as s:
            return list(s.scalars(select(BotCommand).where(BotCommand.status == "PENDING").order_by(BotCommand.id)))

    def complete_command(self, cid: int, status: str, result: str) -> None:
        with self.db.session() as s:
            c = s.get(BotCommand, cid)
            if c:
                c.status, c.result, c.processed_at = status, result, utc_now()

    # --------------------------------------------------------------- symbols
    def upsert_symbols(self, filters: dict[str, SymbolFilters], universe: dict[str, float]) -> None:
        with self.db.session() as s:
            for sym, f in filters.items():
                row = s.get(SymbolInfo, sym)
                vals = dict(tick_size=str(f.tick_size), step_size=str(f.step_size), min_qty=str(f.min_qty),
                            max_qty=str(f.max_qty), market_step_size=str(f.market_step_size),
                            market_max_qty=str(f.market_max_qty), min_notional=f.min_notional,
                            max_leverage=f.max_leverage, maint_margin_rate=f.maint_margin_rate,
                            maint_amount=f.maint_amount, in_universe=sym in universe,
                            quote_volume_24h=universe.get(sym))
                if row is None:
                    s.add(SymbolInfo(symbol=sym, **vals))
                else:
                    for k, v in vals.items():
                        setattr(row, k, v)

    def load_filters(self) -> dict[str, SymbolFilters]:
        with self.db.session() as s:
            return {r.symbol: SymbolFilters(r.symbol, Decimal(r.tick_size), Decimal(r.step_size), Decimal(r.min_qty),
                                            Decimal(r.max_qty), Decimal(r.market_step_size),
                                            Decimal(r.market_max_qty), r.min_notional, r.max_leverage,
                                            r.maint_margin_rate, r.maint_amount)
                    for r in s.scalars(select(SymbolInfo))}

    # --------------------------------------------------------------- signals
    def add_signal(self, *, symbol: str, side: str, candle_time: int, score: int, accepted: bool,
                   reject_reason: str | None, setup: Any, snapshot: dict, components: list[dict]) -> int | None:
        try:
            with self.db.session() as s:
                sig = Signal(symbol=symbol, side=side, candle_time=candle_time, score=score, accepted=accepted,
                             reject_reason=reject_reason, snapshot=_jsonable(snapshot),
                             entry=getattr(setup, "entry", None), stop=getattr(setup, "stop", None),
                             tp1=getattr(setup, "tp1", None), tp2=getattr(setup, "tp2", None),
                             tp3=getattr(setup, "tp3", None))
                s.add(sig)
                s.flush()
                for c in components:
                    s.add(SignalComponent(signal_id=sig.id, name=c["name"], points=c["points"],
                                          max_points=c["max_points"], passed=c["passed"],
                                          mandatory=c.get("mandatory", False), details=_jsonable(c.get("details", {}))))
                return sig.id
        except IntegrityError:
            return None  # aynı mum için sinyal zaten kayıtlı (duplicate koruması)

    def update_signal(self, sig_id: int, **kw: Any) -> None:
        with self.db.session() as s:
            sig = s.get(Signal, sig_id)
            if sig:
                for k, v in kw.items():
                    setattr(sig, k, _jsonable(v) if k == "snapshot" else v)

    def get_signal(self, sig_id: int) -> Signal | None:
        with self.db.session() as s:
            return s.get(Signal, sig_id)

    # ------------------------------------------------------------- positions
    def create_position(self, **kw: Any) -> Position:
        with self.db.session() as s:
            p = Position(**kw)
            s.add(p)
            s.flush()
            return p

    def get_position(self, pos_id: int) -> Position | None:
        with self.db.session() as s:
            return s.get(Position, pos_id)

    def update_position(self, pos_id: int, **kw: Any) -> Position:
        with self.db.session() as s:
            p = s.get(Position, pos_id)
            for k, v in kw.items():
                setattr(p, k, v)
            s.flush()
            return p

    def transition(self, pos_id: int, new_state: PosState, actor: str = "worker", **kw: Any) -> Position:
        with self.db.session() as s:
            p = s.get(Position, pos_id)
            old = p.state
            assert_transition(old, new_state)
            p.state = new_state.value
            for k, v in kw.items():
                setattr(p, k, v)
            if old != new_state.value:
                s.add(AuditLog(actor=actor, action="position_state",
                               details={"position_id": pos_id, "from": old, "to": new_state.value}))
            s.flush()
            return p

    def open_positions(self) -> list[Position]:
        with self.db.session() as s:
            return list(s.scalars(select(Position).where(Position.state.in_([x.value for x in OPEN_STATES]))))

    def closed_positions(self, limit: int = 100) -> list[Position]:
        with self.db.session() as s:
            return list(s.scalars(select(Position).where(Position.state == PosState.CLOSED.value)
                                  .order_by(desc(Position.closed_at)).limit(limit)))

    # ---------------------------------------------------------------- orders
    def add_order(self, **kw: Any) -> Order:
        with self.db.session() as s:
            o = Order(**{k: (_jsonable(v) if k == "raw" else v) for k, v in kw.items()})
            s.add(o)
            s.flush()
            return o

    def update_order(self, client_id: str, **kw: Any) -> None:
        with self.db.session() as s:
            o = s.scalar(select(Order).where(Order.client_id == client_id))
            if o:
                for k, v in kw.items():
                    setattr(o, k, _jsonable(v) if k == "raw" else v)

    def orders_for_position(self, pos_id: int, active_only: bool = False) -> list[Order]:
        with self.db.session() as s:
            q = select(Order).where(Order.position_id == pos_id)
            if active_only:
                q = q.where(Order.active.is_(True))
            return list(s.scalars(q.order_by(Order.id)))

    def active_order(self, pos_id: int, purpose: str) -> Order | None:
        with self.db.session() as s:
            return s.scalar(select(Order).where(Order.position_id == pos_id, Order.purpose == purpose,
                                                Order.active.is_(True)).order_by(desc(Order.id)))

    def add_fills(self, pos_id: int, symbol: str, fills: Iterable[Any]) -> None:
        from backend.smc.core.types import ms_to_dt
        for f in fills:
            try:
                with self.db.session() as s:
                    s.add(Fill(position_id=pos_id, symbol=symbol, exchange_trade_id=f.trade_id,
                               exchange_order_id=f.order_id, side=f.side, price=f.price, qty=f.qty,
                               commission=f.commission, commission_asset=f.commission_asset,
                               realized_pnl=f.realized_pnl, is_maker=f.maker, ts=ms_to_dt(f.time)))
            except IntegrityError:
                pass

    # ---------------------------------------------------------------- trades
    def add_trade(self, **kw: Any) -> Trade:
        with self.db.session() as s:
            existing = s.scalar(select(Trade).where(Trade.position_id == kw["position_id"]))
            if existing:
                return existing
            t = Trade(**kw)
            s.add(t)
            s.flush()
            return t

    def trades(self, limit: int = 200, since: datetime | None = None) -> list[Trade]:
        with self.db.session() as s:
            q = select(Trade)
            if since is not None:
                q = q.where(Trade.exit_time >= since)
            return list(s.scalars(q.order_by(desc(Trade.exit_time)).limit(limit)))

    def pnl_snapshot(self, **kw: Any) -> None:
        with self.db.session() as s:
            s.add(PnlSnapshot(**kw))


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value") and not isinstance(obj, (int, float, str, bool)):
        return obj.value
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj
