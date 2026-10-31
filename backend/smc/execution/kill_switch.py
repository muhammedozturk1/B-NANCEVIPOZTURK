"""Kill switch ve trial bitişinde kullanılan 'flatten' prosedürü.

1. Yeni sinyal/emir durdurulur (çağıran taraf bot state'ini önceden değiştirir)
2. Bekleyen TP/SL emirleri iptal edilir
3. Açık pozisyonlar market (reduceOnly) ile kapatılır
4. Binance state doğrulanır: sıfır pozisyon ve sıfır açık emir
5. DB pozisyonları finalize edilir
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from backend.smc.database.repo import Repo
from backend.smc.execution.gateway import ExchangeGateway
from backend.smc.execution.state_machine import LIVE_STATES, PosState
from backend.smc.execution.trade_manager import TradeManager

log = logging.getLogger(__name__)


@dataclass
class FlattenResult:
    ok: bool
    remaining_positions: list[str] = field(default_factory=list)
    remaining_orders: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


async def flatten_all(gw: ExchangeGateway, repo: Repo, tm: TradeManager, reason: str, *, attempts: int = 10,
                      delay: float = 1.0) -> FlattenResult:
    res = FlattenResult(ok=False)
    db_positions = {p.id: p for p in repo.open_positions()}
    shared = getattr(tm.cfg, "account_scope", "exclusive") == "shared"
    own_symbols = {p.symbol for p in db_positions.values()}

    async def scoped():
        """shared modda yalnızca SMC'nin kendi pozisyonları ve 'sb' önekli emirleri."""
        positions = await gw.positions()
        orders = await gw.open_orders()
        if shared:
            positions = [p for p in positions if p.symbol in own_symbols]
            orders = [o for o in orders if o.client_id.startswith("sb")]
        return positions, orders

    for attempt in range(attempts):
        positions, orders = await scoped()
        symbols = {p.symbol for p in positions} | {o.symbol for o in orders}
        if not positions and not orders:
            res.ok = True
            break
        if shared:  # başka botların emirlerine dokunmamak için tek tek iptal
            for o in orders:
                try:
                    await gw.cancel(o.symbol, o.client_id, o.is_algo)
                except Exception as exc:
                    res.log.append(f"cancel {o.client_id}: {exc!r}")
        else:
            for sym in symbols:
                try:
                    await gw.cancel_all(sym)
                except Exception as exc:
                    res.log.append(f"cancel_all {sym}: {exc!r}")
        for p in positions:
            side = "SELL" if p.qty > 0 else "BUY"
            cid = f"sbK{attempt}{int(time.time() * 1000) % 10**10}"
            try:
                f = None
                try:
                    f = tm.filters(p.symbol)
                except Exception:
                    pass
                await gw.market_reduce(p.symbol, side, abs(p.qty), cid, f)
                res.log.append(f"market close {p.symbol} {p.qty}")
            except Exception as exc:
                res.log.append(f"market close {p.symbol}: {exc!r}")
        await asyncio.sleep(delay)
    else:
        positions, orders = await scoped()
        res.remaining_positions = [f"{p.symbol}:{p.qty}" for p in positions]
        res.remaining_orders = [f"{o.symbol}:{o.client_id}" for o in orders]
        res.ok = not positions and not orders

    for pos in db_positions.values():
        fresh = repo.get_position(pos.id)
        st = PosState(fresh.state)
        if st in LIVE_STATES:
            try:
                if st != PosState.EMERGENCY_EXIT:
                    fresh = repo.transition(pos.id, PosState.EMERGENCY_EXIT)
                await tm.finalize(fresh, reason)
            except Exception as exc:
                res.log.append(f"finalize #{pos.id}: {exc!r}")
        elif st in (PosState.SIGNAL_DETECTED, PosState.RISK_CHECK, PosState.ORDER_SUBMITTED):
            repo.update_position(pos.id, state=PosState.CLOSED.value, exit_reason=reason, open_risk=0.0)
    return res
