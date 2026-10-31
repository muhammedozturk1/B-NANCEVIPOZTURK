"""Execution Engine + Position Manager.

Akış: ENTRY (limit, timeout, partial fill) → NATIVE SL (STOP_MARKET) → TP1/TP2/TP3 (TAKE_PROFIT_MARKET)
→ TP1 sonrası BREAKEVEN → TP2 sonrası SL=TP1 (opsiyonel) → EXIT → PnL → DB → Telegram.

Tüm koruma emirleri Binance üzerinde native olarak durur; bot süreci kapansa bile pozisyon korunur.
SL yerleştirilemezse pozisyon market emriyle güvenli şekilde kapatılır ve bot SAFE_MODE'a geçer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from backend.smc.config import Settings
from backend.smc.core.types import Side, ensure_utc, utc_now
from backend.smc.database.models import Position
from backend.smc.database.repo import Repo
from backend.smc.execution.gateway import ExchangeGateway, ExchangePosition, OrderInfo
from backend.smc.execution.state_machine import LIVE_STATES, PosState
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.notifications.telegram import TelegramNotifier
from backend.smc.risk.engine import RiskDecision, register_trade_result
from backend.smc.risk.plan import TPLevel, open_risk, plan_take_profits, planned_rr, r_multiple, stop_after_tp
from backend.smc.risk.sizing import per_unit_loss
from backend.smc.strategy.signal import TradeSetup

log = logging.getLogger(__name__)
S = PosState
TRIGGERED = {"TRIGGERED", "FINISHED", "FILLED"}
EPS = 1e-12


@dataclass
class CloseSummary:
    gross: float
    commission: float
    funding: float
    slippage: float
    net: float
    exit_price: float | None


class TradeManager:
    def __init__(self, gw: ExchangeGateway, repo: Repo, notifier: TelegramNotifier, cfg: Settings,
                 filters: Callable[[str], SymbolFilters], on_safe_mode: Callable[[str, bool], Awaitable[None]],
                 clock: Callable[[], datetime] = utc_now, poll_interval: float = 0.5):
        self.gw = gw
        self.repo = repo
        self.notifier = notifier
        self.cfg = cfg
        self.filters = filters
        self.on_safe_mode = on_safe_mode
        self.clock = clock
        self.poll = poll_interval
        self.busy: set[int] = set()
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- utils
    @staticmethod
    def cid(pos_id: int, tag: str) -> str:
        """Benzersiz client id (≤36 karakter, Binance regex uyumlu)."""
        return f"sb{pos_id}{tag}{int(time.time() * 1000) % 10**9}"

    def _record_order(self, pos: Position, cid: str, purpose: str, side: str, otype: str, qty: float,
                      price: float | None = None, trigger: float | None = None, is_algo: bool = False) -> None:
        self.repo.add_order(position_id=pos.id, symbol=pos.symbol, client_id=cid, purpose=purpose, side=side,
                            type=otype, qty=qty, price=price, trigger_price=trigger, is_algo=is_algo, status="NEW")

    def _sync_order(self, o: OrderInfo, **extra) -> None:
        self.repo.update_order(o.client_id, exchange_id=o.exchange_id, status=o.status, filled_qty=o.filled_qty,
                               avg_price=o.avg_price or None, active=o.active, raw=o.raw, **extra)

    @property
    def _algo(self) -> bool:
        return bool(getattr(self.gw, "use_algo", True))

    # ------------------------------------------------------------------ open
    async def open_position(self, setup: TradeSetup, decision: RiskDecision, signal_id: int | None) -> Position | None:
        sizing = decision.sizing
        assert sizing is not None and decision.stop is not None
        side = setup.side
        f = self.filters(setup.symbol)
        levels = decision.tp_levels
        pos = self.repo.create_position(
            signal_id=signal_id, symbol=setup.symbol, side=side.value, state=S.SIGNAL_DETECTED.value,
            leverage=sizing.leverage, planned_entry=setup.entry, requested_qty=sizing.qty,
            initial_stop=decision.stop, stop_price=decision.stop, tp1_price=_lvl(levels, "TP1"),
            tp2_price=_lvl(levels, "TP2"), tp3_price=_lvl(levels, "TP3"), tp_plan=[l.as_dict() for l in levels],
            risk_amount=sizing.risk_actual, open_risk=sizing.risk_actual, liq_price=sizing.liq_price,
            signal_score=setup.score)
        self.busy.add(pos.id)
        try:
            return await self._open(pos, setup, decision, f)
        finally:
            self.busy.discard(pos.id)

    async def _open(self, pos: Position, setup: TradeSetup, decision: RiskDecision, f: SymbolFilters) -> Position | None:
        side = setup.side
        sizing = decision.sizing
        self.repo.transition(pos.id, S.RISK_CHECK)
        try:
            await self.gw.ensure_symbol_setup(pos.symbol, sizing.leverage)
        except Exception as exc:
            self.repo.transition(pos.id, S.ORDER_REJECTED, exit_reason="SETUP_FAILED", closed_at=self.clock())
            self.repo.system("ERROR", "symbol_setup_failed", str(exc), symbol=pos.symbol)
            self.notifier.notify("ORDER_REJECTED", f"{pos.symbol} margin/leverage ayarlanamadı: {exc}")
            return None

        limit_px = setup.entry * (1 + side.sign * self.cfg.entry_limit_offset_pct)
        cid = self.cid(pos.id, "E")
        self._record_order(pos, cid, "ENTRY", side.entry_order_side, "LIMIT", sizing.qty, price=limit_px)
        try:
            o = await self.gw.place_limit(pos.symbol, side.entry_order_side, sizing.qty, limit_px, cid, f)
        except Exception as exc:
            self.repo.update_order(cid, status="REJECTED", active=False, raw={"error": str(exc)})
            self.repo.transition(pos.id, S.ORDER_REJECTED, exit_reason="ORDER_REJECTED", closed_at=self.clock())
            self.notifier.notify("ORDER_REJECTED", f"{pos.symbol} {side.value} entry reddedildi: {exc}")
            return None
        self._sync_order(o)
        self.repo.transition(pos.id, S.ORDER_SUBMITTED)

        o = await self._wait_fill(pos.symbol, cid, o)
        if o.active:  # timeout: kalan miktarı iptal et (full fill varsayımı yok)
            try:
                await self.gw.cancel(pos.symbol, cid)
            except Exception as exc:
                log.warning("entry cancel failed: %r", exc)
            o = await self.gw.get_order(pos.symbol, cid) or o
        self._sync_order(o)
        filled, avg = o.filled_qty, (o.avg_price or setup.entry)
        if filled <= EPS:
            self.repo.transition(pos.id, S.CLOSED, exit_reason="ENTRY_NOT_FILLED", closed_at=self.clock(),
                                 open_risk=0.0)
            return None
        if filled < sizing.qty - EPS:
            self.repo.transition(pos.id, S.PARTIALLY_FILLED, filled_qty=filled)
            self.repo.system("WARNING", "partial_fill", f"{pos.symbol} {filled}/{sizing.qty}", position_id=pos.id)

        stop = decision.stop
        pul = per_unit_loss(side, avg, stop, self.cfg)[0]
        levels = plan_take_profits(side, avg, stop, filled, f, self.cfg)
        pos = self.repo.transition(
            pos.id, S.ENTRY_FILLED, entry_price=avg, filled_qty=filled, remaining_qty=filled, notional=filled * avg,
            entry_filled_at=self.clock(), risk_amount=filled * pul, open_risk=open_risk(side, avg, stop, filled, self.cfg),
            tp_plan=[l.as_dict() for l in levels], tp1_price=_lvl(levels, "TP1"), tp2_price=_lvl(levels, "TP2"),
            tp3_price=_lvl(levels, "TP3"))

        # ---- native SL (zorunlu)
        if not await self._place_sl(pos, stop, filled):
            self.repo.transition(pos.id, S.SL_PLACEMENT_FAILED)
            self.notifier.notify("SL_FAILURE", f"{pos.symbol} SL yerleştirilemedi → pozisyon kapatılıyor")
            await self.emergency_close(self.repo.get_position(pos.id), "SL_FAILURE")
            await self.on_safe_mode("sl_placement_failed", True)
            return None
        self.repo.transition(pos.id, S.SL_PLACED)

        # ---- native TP'ler
        tp_ok = await self._place_tps(pos, levels)
        if tp_ok:
            self.repo.transition(pos.id, S.TP_ORDERS_PLACED)
        else:
            self.repo.transition(pos.id, S.TP_PLACEMENT_FAILED, tp_missing=True)
            self.notifier.notify("TP_FAILURE", f"{pos.symbol} TP emirleri eksik; SL aktif, yeniden denenecek")
        pos = self.repo.transition(pos.id, S.ACTIVE)
        rr = planned_rr(side, avg, stop, levels, self.cfg)
        self.notifier.notify(
            "NEW_LONG" if side is Side.LONG else "NEW_SHORT",
            f"{pos.symbol} {side.value} @ {avg:g} qty {filled:g} lev {pos.leverage}x | SL {stop:g} | "
            f"TP {', '.join(f'{l.price:g}' for l in levels)} | risk {pos.risk_amount:.2f} USDT | "
            f"skor {setup.score}/15 | RR {rr:.2f}")
        return pos

    async def _wait_fill(self, symbol: str, cid: str, o: OrderInfo) -> OrderInfo:
        deadline = time.monotonic() + self.cfg.entry_order_timeout_s
        while o.active and time.monotonic() < deadline:
            await asyncio.sleep(self.poll)
            try:
                o = await self.gw.get_order(symbol, cid) or o
            except Exception as exc:
                log.warning("order poll failed: %r", exc)
        return o

    async def _place_sl(self, pos: Position, stop: float, qty: float) -> bool:
        side = Side(pos.side)
        f = self.filters(pos.symbol)
        for attempt in range(self.cfg.order_retry_attempts):
            cid = self.cid(pos.id, "S")
            self._record_order(pos, cid, "SL", side.exit_order_side, "STOP_MARKET", qty, trigger=stop,
                               is_algo=self._algo)
            try:
                o = await self.gw.place_stop(pos.symbol, side.exit_order_side, qty, stop, cid, f)
                self._sync_order(o)
                self.repo.update_position(pos.id, stop_price=stop)
                return True
            except Exception as exc:
                self.repo.update_order(cid, status="REJECTED", active=False, raw={"error": str(exc)})
                self.repo.system("ERROR", "sl_placement_error", str(exc), position_id=pos.id, attempt=attempt)
                if getattr(exc, "code", None) == -2021:  # anında tetiklenecek: tekrar denemenin anlamı yok
                    return False
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

    async def _place_tps(self, pos: Position, levels: list[TPLevel]) -> bool:
        side = Side(pos.side)
        f = self.filters(pos.symbol)
        ok = True
        existing = {o.purpose for o in self.repo.orders_for_position(pos.id, active_only=True)}
        for lvl in levels:
            if lvl.filled or lvl.name in existing:
                continue
            cid = self.cid(pos.id, lvl.name.replace("TP", "T"))
            self._record_order(pos, cid, lvl.name, side.exit_order_side, "TAKE_PROFIT_MARKET", lvl.qty,
                               trigger=lvl.price, is_algo=self._algo)
            try:
                o = await self.gw.place_take_profit(pos.symbol, side.exit_order_side, lvl.qty, lvl.price, cid, f)
                self._sync_order(o)
            except Exception as exc:
                ok = False
                self.repo.update_order(cid, status="REJECTED", active=False, raw={"error": str(exc)})
                self.repo.system("ERROR", "tp_placement_error", str(exc), position_id=pos.id, level=lvl.name)
        return ok

    async def replace_stop(self, pos: Position, new_stop: float, qty: float) -> bool:
        """Önce yeni SL'yi koy, sonra eskisini iptal et (korumasız an olmaz; ikisi de reduceOnly)."""
        old = self.repo.active_order(pos.id, "SL")
        if not await self._place_sl(pos, new_stop, qty):
            return False
        if old is not None:
            try:
                await self.gw.cancel(pos.symbol, old.client_id, old.is_algo)
            except Exception as exc:
                log.warning("eski SL iptal edilemedi: %r", exc)
            self.repo.update_order(old.client_id, active=False, status="CANCELED")
        return True

    async def _cancel_own(self, symbol: str) -> None:
        """Sembolde SMC'ye ait ('sb' önekli) açık emirleri iptal eder.
        exclusive modda ek olarak sembolün tüm emirleri temizlenir; shared modda başka
        botların emirlerine dokunulmaz."""
        try:
            for o in await self.gw.open_orders(symbol):
                if o.client_id.startswith("sb"):
                    await self.gw.cancel(symbol, o.client_id, o.is_algo)
            if getattr(self.cfg, "account_scope", "exclusive") == "exclusive":
                await self.gw.cancel_all(symbol)
        except Exception as exc:
            log.warning("emir iptali başarısız %s: %r", symbol, exc)

    # ---------------------------------------------------------------- manage
    async def manage_all(self) -> None:
        async with self._lock:
            live = [p for p in self.repo.open_positions() if PosState(p.state) in LIVE_STATES and p.id not in self.busy]
            if not live:
                return
            ex = {p.symbol: p for p in await self.gw.positions()}
            for pos in live:
                try:
                    await self.manage(pos, ex.get(pos.symbol))
                except Exception as exc:
                    self.repo.system("ERROR", "manage_error", repr(exc), position_id=pos.id)

    async def manage(self, pos: Position, ex: ExchangePosition | None) -> None:
        side = Side(pos.side)
        if PosState(pos.state) == S.RECONCILIATION_REQUIRED:
            return  # reconciliation çözülene kadar otomatik müdahale yok (native SL/TP borsada duruyor)
        ex_qty = abs(ex.qty) if ex is not None and (ex.qty > 0) == (side is Side.LONG) else 0.0
        if ex is not None and ex_qty == 0.0:
            self.repo.transition(pos.id, S.RECONCILIATION_REQUIRED)
            self.repo.system("ERROR", "position_side_mismatch", pos.symbol, position_id=pos.id)
            self.notifier.notify("RECONCILIATION", f"{pos.symbol} borsadaki pozisyon yönü DB ile uyuşmuyor")
            return
        if ex_qty <= EPS:
            reason = await self._exit_reason(pos)
            await self.finalize(pos, reason)
            return

        f = self.filters(pos.symbol)
        step = float(f.step_size)
        if ex_qty > pos.remaining_qty + step / 2:
            self.repo.transition(pos.id, S.RECONCILIATION_REQUIRED)
            self.notifier.notify("RECONCILIATION", f"{pos.symbol} borsa miktarı ({ex_qty}) DB'den büyük")
            return

        levels = [TPLevel(**l) for l in pos.tp_plan]
        closed_qty = pos.filled_qty - ex_qty
        cum, newly = 0.0, []
        for l in levels:
            cum += l.qty
            if not l.filled and closed_qty >= cum - step / 2:
                l.filled = True
                newly.append(l.name)
        if newly or abs(ex_qty - pos.remaining_qty) > step / 2:
            pos = self.repo.update_position(pos.id, remaining_qty=ex_qty, tp_plan=[l.as_dict() for l in levels])
            for name in newly:
                pos = self.repo.transition(pos.id, S(f"{name}_FILLED"))
                self.notifier.notify(name, f"{pos.symbol} {name} gerçekleşti, kalan {ex_qty:g}")
            filled_names = {l.name for l in levels if l.filled}
            new_stop = stop_after_tp(side, pos.entry_price, levels, filled_names, pos.stop_price, self.cfg, f.tick_size)
            if new_stop != pos.stop_price:
                if await self.replace_stop(pos, new_stop, ex_qty):
                    pos = self.repo.get_position(pos.id)
                    if "TP1" in newly and not pos.be_active:
                        pos = self.repo.update_position(pos.id, be_active=True)
                        if PosState(pos.state) == S.TP1_FILLED:
                            pos = self.repo.transition(pos.id, S.BREAKEVEN_ACTIVE)
                        self.notifier.notify("BREAKEVEN", f"{pos.symbol} SL breakeven'a taşındı: {new_stop:g}")
                else:
                    self.notifier.notify("SL_FAILURE", f"{pos.symbol} SL taşınamadı; mevcut SL korunuyor")

        # SL hâlâ borsada mı?
        open_ids = {o.client_id for o in await self.gw.open_orders(pos.symbol)}
        sl = self.repo.active_order(pos.id, "SL")
        if sl is None or sl.client_id not in open_ids:
            if sl is not None:
                info = await self.gw.get_order(pos.symbol, sl.client_id, sl.is_algo)
                if info is not None and info.status in TRIGGERED:
                    return  # SL tetiklendi; pozisyon bir sonraki döngüde kapanmış görünecek
                self.repo.update_order(sl.client_id, active=False, status=info.status if info else "MISSING")
            self.repo.system("ERROR", "sl_missing", f"{pos.symbol} SL bulunamadı, yeniden yerleştiriliyor",
                             position_id=pos.id)
            if not await self._place_sl(pos, pos.stop_price, ex_qty):
                self.notifier.notify("SL_FAILURE", f"{pos.symbol} SL yeniden yerleştirilemedi → acil kapanış")
                await self.emergency_close(pos, "SL_FAILURE")
                await self.on_safe_mode("sl_placement_failed", True)
                return
        # TP aktif olmayan TP emirlerini temizle / eksikleri tamamla
        for o in self.repo.orders_for_position(pos.id, active_only=True):
            if o.purpose.startswith("TP") and o.client_id not in open_ids:
                self.repo.update_order(o.client_id, active=False, status="DONE")
        if pos.tp_missing:
            if await self._place_tps(pos, [l for l in levels if not l.filled]):
                pos = self.repo.update_position(pos.id, tp_missing=False)
        self.repo.update_position(pos.id, open_risk=open_risk(side, pos.entry_price, pos.stop_price, ex_qty, self.cfg))

    async def _exit_reason(self, pos: Position) -> str:
        orders = self.repo.orders_for_position(pos.id)
        for purpose in ("TP3", "SL", "TP2", "TP1"):
            cands = [o for o in orders if o.purpose == purpose]
            for o in reversed(cands):
                try:
                    info = await self.gw.get_order(pos.symbol, o.client_id, o.is_algo)
                except Exception:
                    info = None
                if info is not None and info.status in TRIGGERED:
                    if purpose == "SL":
                        if pos.be_active:
                            return "BREAKEVEN_STOP"
                        return "SL"
                    return purpose
        return "EXTERNAL"

    # -------------------------------------------------------------- closing
    async def emergency_close(self, pos: Position, reason: str) -> None:
        side = Side(pos.side)
        if PosState(pos.state) != S.EMERGENCY_EXIT:
            pos = self.repo.transition(pos.id, S.EMERGENCY_EXIT)
        await self._cancel_own(pos.symbol)
        for _ in range(5):
            ex = next((p for p in await self.gw.positions() if p.symbol == pos.symbol), None)
            if ex is None or ex.qty == 0:
                break
            cid = self.cid(pos.id, "X")
            self._record_order(pos, cid, "EXIT", side.exit_order_side, "MARKET", abs(ex.qty))
            try:
                o = await self.gw.market_reduce(pos.symbol, side.exit_order_side, abs(ex.qty), cid,
                                                self.filters(pos.symbol))
                self._sync_order(o)
            except Exception as exc:
                self.repo.system("ERROR", "emergency_close_error", str(exc), position_id=pos.id)
            await asyncio.sleep(self.poll)
        await self.finalize(self.repo.get_position(pos.id), reason)

    async def finalize(self, pos: Position, reason: str) -> None:
        side = Side(pos.side)
        await self._cancel_own(pos.symbol)
        for o in self.repo.orders_for_position(pos.id, active_only=True):
            self.repo.update_order(o.client_id, active=False)
        summary = await self._summarize(pos)
        now = self.clock()
        if reason == "TP3" and PosState(pos.state) != S.TP3_FILLED:
            try:
                pos = self.repo.transition(pos.id, S.TP3_FILLED)
            except Exception:
                pass
        hits = [l["name"] for l in pos.tp_plan if l.get("filled")] + (["TP3"] if reason == "TP3" else [])
        pos = self.repo.transition(pos.id, S.CLOSED, exit_reason=reason, closed_at=now, remaining_qty=0.0,
                                   open_risk=0.0)
        if pos.entry_price:
            self.repo.add_trade(
                position_id=pos.id, symbol=pos.symbol, side=pos.side, entry_price=pos.entry_price,
                entry_time=ensure_utc(pos.entry_filled_at or pos.opened_at), quantity=pos.filled_qty,
                notional=pos.notional or 0.0, leverage=pos.leverage, stop_price=pos.initial_stop,
                tp1_price=pos.tp1_price, tp2_price=pos.tp2_price, tp3_price=pos.tp3_price,
                risk_amount=pos.risk_amount,
                planned_rr=planned_rr(side, pos.entry_price, pos.initial_stop,
                                      [TPLevel(**l) for l in pos.tp_plan], self.cfg) if pos.tp_plan else None,
                realized_rr=r_multiple(summary.net, pos.risk_amount), gross_pnl=summary.gross,
                net_pnl=summary.net, commission=summary.commission, funding=summary.funding,
                slippage=summary.slippage, exit_price=summary.exit_price, exit_time=now, exit_reason=reason,
                signal_score=pos.signal_score, tp_hits=list(dict.fromkeys(hits)))
        st = self.repo.state()
        consec, cooldown = register_trade_result(st.consecutive_losses, summary.net, reason, now, self.cfg)
        upd = {"consecutive_losses": consec}
        if cooldown is not None:
            upd["cooldown_until"] = cooldown
            self.notifier.notify("CONSECUTIVE_LOSSES", f"{self.cfg.max_consecutive_sl} ardışık SL → "
                                                       f"{self.cfg.cooldown_minutes} dk cooldown")
            self.repo.risk_event("consecutive_losses_cooldown", until=cooldown.isoformat())
        self.repo.update_state(**upd)
        kind = "SL" if reason == "SL" else ("TP3" if reason == "TP3" else "CLOSED")
        self.notifier.notify(kind, f"{pos.symbol} {pos.side} kapandı ({reason}) | net {summary.net:+.4f} USDT | "
                                   f"R {r_multiple(summary.net, pos.risk_amount):+.2f} | komisyon "
                                   f"{summary.commission:.4f} | funding {summary.funding:+.4f}")

    async def _summarize(self, pos: Position) -> CloseSummary:
        start = ensure_utc(pos.opened_at)
        start_ms = int(start.timestamp() * 1000) - 5_000 if start else 0
        try:
            fills = await self.gw.fills_since(pos.symbol, start_ms)
        except Exception as exc:
            self.repo.system("WARNING", "fills_unavailable", repr(exc), position_id=pos.id)
            fills = []
        try:
            funding = await self.gw.funding_since(pos.symbol, start_ms)
        except Exception:
            funding = 0.0
        self.repo.add_fills(pos.id, pos.symbol, fills)
        side = Side(pos.side)
        gross = sum(f.realized_pnl for f in fills)
        commission = sum(f.commission for f in fills if f.commission_asset in ("USDT", "", None))
        exits = [f for f in fills if f.side == side.exit_order_side]
        entries = [f for f in fills if f.side == side.entry_order_side]
        exit_q = sum(f.qty for f in exits)
        exit_px = sum(f.price * f.qty for f in exits) / exit_q if exit_q else None
        slip = 0.0
        if entries and pos.planned_entry:
            eq = sum(f.qty for f in entries)
            evwap = sum(f.price * f.qty for f in entries) / eq
            slip += max(0.0, (evwap - pos.planned_entry) * side.sign) * eq
        for f in exits:  # stop tetiklenmesindeki kayma (yalnızca stop altı/üstü dolumlar)
            if (f.price - pos.stop_price) * side.sign < 0 and pos.stop_price:
                slip += abs(f.price - pos.stop_price) * f.qty
        # Not: gerçekleşmiş PnL dolum fiyatlarını kullandığı için slippage zaten içindedir;
        # 'slippage' alanı analiz amaçlı ayrıca raporlanır ve net PnL'den ikinci kez düşülmez.
        net = gross - commission + funding
        return CloseSummary(gross, commission, funding, slip, net, exit_px)


def _lvl(levels: list[TPLevel], name: str) -> float | None:
    return next((l.price for l in levels if l.name == name), None)
