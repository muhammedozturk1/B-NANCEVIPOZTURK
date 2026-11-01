"""Trading Worker — Render 'Background Worker' olarak sürekli çalışır.

Katmanlar: Market Data → Strategy Engine → Signal Engine → Risk Engine → Position Sizing →
Execution Engine → Position Manager → Database → Dashboard / Telegram.

Dashboard ayrı bir servistir; worker'a yalnızca DB'deki bot_commands kuyruğu üzerinden komut verir.
Dashboard çökse bile worker ve Binance üzerindeki native SL/TP emirleri çalışmaya devam eder.
"""
from __future__ import annotations

import asyncio
import logging
import signal as _signal
import time

from sqlalchemy import func, select

from backend.smc.config import Settings, get_settings
from backend.smc.core.trial import ensure_trial, trial_status
from backend.smc.core.types import Side, TIMEFRAME_MS, Candle, ensure_utc, now_ms, utc_now
from backend.smc.database.db import Database
from backend.smc.database.models import MarketSnapshot, TrialState, Trade
from backend.smc.database.repo import Repo
from backend.smc.execution.gateway import AccountInfo, BinanceGateway
from backend.smc.execution.kill_switch import flatten_all
from backend.smc.execution.reconciliation import reconcile
from backend.smc.execution.state_machine import LIVE_STATES, BotStatus, PosState
from backend.smc.execution.trade_manager import TradeManager
from backend.smc.market_data.candles import CandleStore
from backend.smc.market_data.rest import BinanceAPIError, BinanceREST
from backend.smc.market_data.sentiment import FearGreedProvider
from backend.smc.market_data.universe import (SymbolFilters, apply_brackets, book_metrics, build_universe,
                                      eligible_perpetuals)
from backend.smc.market_data.ws import (ReconnectingWebSocket, WSState, market_stream_url, pick_variant,
                                       user_stream_url)
from backend.smc.notifications.telegram import TelegramNotifier
from backend.smc.risk.correlation import correlation
from backend.smc.risk.engine import (Candidate, OpenPositionView, PortfolioState, TradingGate, cooldown_active,
                             daily_loss_breached, evaluate_trade)
from backend.smc.strategy.orderflow import OpenInterestTracker
from backend.smc.strategy.signal import MarketContext, components_as_dicts, evaluate_symbol

log = logging.getLogger("worker")
TFS = ("3m", "5m", "15m")
AUTO_SAFE = {"rest", "db", "clock", "market_data", "position_mode"}


class TradingWorker:
    def __init__(self, s: Settings, db: Database | None = None):
        self.s = s
        self.db = db or Database(s.database_url)
        self.db.create_all()
        self.repo = Repo(self.db)
        self.rest = BinanceREST(s.binance_rest_url, s.binance_api_key.get_secret_value(),
                                s.binance_api_secret.get_secret_value(), recv_window=s.recv_window_ms,
                                weight_per_min=s.rate_limit_weight_per_min, orders_per_10s=s.rate_limit_orders_per_10s,
                                orders_per_min=s.rate_limit_orders_per_min)
        self.public = BinanceREST(s.public_data_rest_url, weight_per_min=1000)
        self.gw = BinanceGateway(self.rest, use_algo=s.use_algo_orders, working_type=s.sl_working_type,
                                 margin_type=s.margin_mode, attempts=s.order_retry_attempts)
        self.notifier = TelegramNotifier(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id, self.repo,
                                         env_label=f"SMC·{s.trading_env.upper()}")
        self.candles = CandleStore()
        self.oi = OpenInterestTracker()
        self.fng = FearGreedProvider(s.fng_url) if s.fng_enabled else None
        self.filters: dict[str, SymbolFilters] = {}
        self.universe: list[str] = []
        self.marks: dict[str, float] = {}
        self.funding: dict[str, float] = {}
        self.account: AccountInfo | None = None
        self.unrealized_bot = 0.0
        self.foreign_symbols: set[str] = set()  # başka bir bota ait pozisyonu olan semboller (shared)
        self.heartbeat_hook = None  # mevcut botun bot_status tablosuna canlılık yazmak için
        self.safe_reasons: set[str] = set()
        self.holds: set[str] = set()  # manuel RESUME gerektiren
        self.status = BotStatus.INITIALIZING
        self.tm = TradeManager(self.gw, self.repo, self.notifier, s, self._filter, self.enter_safe_mode)
        self.ws_market = ReconnectingWebSocket("market", self._market_url, self._on_market,
                                               on_state=self._on_ws_state, on_connected=self._on_market_connected)
        self.ws_user = ReconnectingWebSocket("user", self._user_url, self._on_user, on_state=self._on_ws_state,
                                             on_connected=self._on_user_connected, recv_timeout=None)
        self._url_variant = {"market": 0, "user": 0}  # son başarılı URL biçimi
        self._url_in_use = {"market": 0, "user": 0}
        self._backfill_task: asyncio.Task | None = None
        self._scan_queue: asyncio.Queue[int] = asyncio.Queue()
        self._last_scanned = 0
        self._manage_now = asyncio.Event()
        self._alert_throttle: dict[str, float] = {}
        self._ws_was_connected: dict[str, bool] = {}
        self._daily_alerted_for: str | None = None
        self._stop = asyncio.Event()

    # =============================================================== helpers
    def _filter(self, symbol: str) -> SymbolFilters:
        return self.filters[symbol]

    def _throttled(self, key: str, seconds: float = 1800) -> bool:
        now = time.monotonic()
        if now - self._alert_throttle.get(key, -1e9) < seconds:
            return True
        self._alert_throttle[key] = now
        return False

    async def enter_safe_mode(self, reason: str, manual: bool = False) -> None:
        target = self.holds if manual else self.safe_reasons
        if reason not in target:
            target.add(reason)
            self.repo.system("ERROR", "safe_mode", reason, manual=manual)
            self.notifier.notify("SAFE_MODE", f"SAFE MODE: {reason}" + (" (manuel RESUME gerekli)" if manual else ""))
            self._persist_holds()
        self._refresh_status()

    def _clear_safe(self, reason: str) -> None:
        if reason in self.safe_reasons:
            self.safe_reasons.discard(reason)
            self.repo.system("INFO", "safe_mode_cleared", reason)
            self._refresh_status()

    def _persist_holds(self) -> None:
        self.repo.update_state(manual_hold_reasons=sorted(self.holds), safe_mode_reasons=sorted(self.safe_reasons))

    def trial_expired(self) -> bool:
        with self.db.session() as sess:
            row = sess.get(TrialState, 1)
            return row is not None and trial_status(row, utc_now()).expired

    def _refresh_status(self) -> BotStatus:
        st = self.repo.state()
        if "expired" in self.holds:
            status = BotStatus.EXPIRED
        elif "kill" in self.holds:
            status = BotStatus.EMERGENCY_STOP
        elif "reconciliation" in self.holds:
            status = BotStatus.RECONCILIATION_REQUIRED
        elif self.holds or self.safe_reasons:
            status = BotStatus.SAFE_MODE
        elif self.status == BotStatus.INITIALIZING and not self._ready_flag:
            status = BotStatus.INITIALIZING
        else:
            pf = self._portfolio() if self.account else None
            if pf and daily_loss_breached(pf, self.s):
                status = BotStatus.STOP_NEW_TRADES
            elif pf and cooldown_active(pf):
                status = BotStatus.COOLDOWN
            else:
                status = BotStatus.RUNNING
        if status != self.status:
            self.repo.audit("worker", "bot_status", old=self.status.value, new=status.value)
        self.status = status
        reason = ", ".join(sorted(self.holds | self.safe_reasons)) or None
        if status in (BotStatus.RUNNING, BotStatus.COOLDOWN) and self.s.require_ws_connected:
            missing = [n for n, w in (("market", self.ws_market), ("user", self.ws_user)) if not w.ready]
            if missing:
                reason = f"yeni işlem bekliyor: websocket ({'/'.join(missing)}) bağlı değil"
        if st.state != status.value or st.reason != reason:
            self.repo.update_state(state=status.value, reason=reason)
        return status

    _ready_flag = False

    def gate(self) -> TradingGate:
        if self.status in (BotStatus.EXPIRED, BotStatus.EMERGENCY_STOP, BotStatus.RECONCILIATION_REQUIRED,
                           BotStatus.SAFE_MODE, BotStatus.INITIALIZING):
            return TradingGate(False, self.status.value)
        if self.trial_expired():
            return TradingGate(False, "trial_expired")
        if self.s.require_ws_connected and not (self.ws_market.ready and self.ws_user.ready):
            return TradingGate(False, "websocket_not_ready")
        if not self.rest.time.ok:
            return TradingGate(False, "clock_not_synced")
        return TradingGate(True)

    # ============================================================== equity
    def _bot_realized_since_start(self) -> float:
        with self.db.session() as sess:
            return float(sess.scalar(select(func.coalesce(func.sum(Trade.net_pnl), 0.0))) or 0.0)

    def _portfolio(self) -> PortfolioState:
        st = self.repo.state()
        positions = [p for p in self.repo.open_positions() if PosState(p.state) in LIVE_STATES]
        acct = self.account
        if self.s.equity_mode == "virtual" or acct is None:
            equity = self.s.starting_equity + self._bot_realized_since_start() + self.unrealized_bot
            margin = sum((p.remaining_qty * (p.entry_price or p.planned_entry)) / max(1, p.leverage) for p in positions)
            available = max(0.0, equity - margin)
            if acct is not None:
                available = min(available, acct.available_balance)
        else:
            equity, available = acct.equity, acct.available_balance
        return PortfolioState(
            equity=equity, available_balance=available, day_start_equity=st.day_start_equity or equity,
            open_positions=[OpenPositionView(p.symbol, Side(p.side), p.open_risk) for p in positions],
            consecutive_losses=st.consecutive_losses, cooldown_until=ensure_utc(st.cooldown_until), now=utc_now())

    async def refresh_account(self) -> None:
        try:
            self.account = await self.gw.account()
            managed = {p.symbol for p in self.repo.open_positions()}
            ex_positions = await self.gw.positions()
            self.unrealized_bot = sum(p.unrealized_pnl for p in ex_positions if p.symbol in managed)
            self.foreign_symbols = {p.symbol for p in ex_positions if p.symbol not in managed} \
                if self.s.account_scope == "shared" else set()
            self._clear_safe("rest")
        except Exception as exc:
            log.warning("account refresh failed: %r", exc)
            if self.rest.consecutive_failures >= 5:
                await self.enter_safe_mode("rest")
            return
        pf = self._portfolio()
        st = self.repo.state()
        now = utc_now()
        day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        upd: dict = dict(equity=pf.equity, wallet_balance=self.account.wallet_balance,
                         available_balance=pf.available_balance, unrealized_pnl=self.unrealized_bot
                         if self.s.equity_mode == "virtual" else self.account.unrealized_pnl,
                         margin_used=sum(p.remaining_qty * (p.entry_price or 0) / max(1, p.leverage)
                                         for p in self.repo.open_positions()))
        if st.day_key != day:
            upd.update(day_key=day, day_start_equity=pf.equity)
        if st.month_key != month:
            upd.update(month_key=month, month_start_equity=pf.equity)
        self.repo.update_state(**upd)
        if st.day_key == day and daily_loss_breached(self._portfolio(), self.s) and self._daily_alerted_for != day:
            self._daily_alerted_for = day
            self.repo.risk_event("daily_loss_limit", loss=pf.day_start_equity - pf.equity)
            self.notifier.notify("DAILY_LOSS", f"Günlük zarar limiti (%{self.s.daily_loss_limit * 100:.0f}) aşıldı → "
                                               f"STOP NEW TRADES (gün sonuna kadar)")
        self._refresh_status()

    # ============================================================== startup
    async def bootstrap(self) -> None:
        now = utc_now()
        st = self.repo.state()
        restart = st.worker_started_at is not None
        self.holds = set(st.manual_hold_reasons or [])
        self.repo.update_state(worker_started_at=now, state=BotStatus.INITIALIZING.value)
        self.repo.save_config("effective", self.s.public_dict())

        # Önce Binance bağlantısını ve API key'i doğrula: key yanlışsa / testnet erişilemiyorsa
        # 72 saatlik trial BAŞLAMAZ (süre boşa akmaz).
        await self.rest.sync_time()
        await self.gw.account()

        now = utc_now()
        with self.db.session() as sess:
            had_trial = sess.get(TrialState, 1) is not None
            row = ensure_trial(sess, now, self.s.trial_hours)
            ts = trial_status(row, now)
        if not had_trial:
            self.notifier.notify("TRIAL_START", f"72 saatlik trial başladı. Bitiş: {ts.expires_at:%Y-%m-%d %H:%M} UTC")
            self.repo.audit("worker", "trial_start", expires_at=ts.expires_at.isoformat())
        self.notifier.notify("RESTART" if restart else "STARTUP",
                             f"Worker {'yeniden başladı' if restart else 'başladı'} | trial kalan "
                             f"{ts.remaining_s / 3600:.1f} saat | durum hold={sorted(self.holds) or '-'}")

        info = await self.rest.exchange_info()
        filters = eligible_perpetuals(info, self.s)
        try:
            filters = apply_brackets(filters, await self.rest.leverage_brackets(), self.s.max_leverage)
        except BinanceAPIError as exc:
            self.repo.system("WARNING", "brackets_unavailable", str(exc))
        self.filters = filters
        if await self.rest.get_position_mode():
            try:
                await self.rest.set_one_way_mode()
            except BinanceAPIError as exc:
                await self.enter_safe_mode("position_mode")
                self.repo.system("ERROR", "position_mode", f"ONE-WAY moda geçilemedi: {exc}")
        await self.refresh_universe(initial=True)

        if ts.expired and "expired" not in self.holds:
            await self.expire()
            return
        report = await reconcile(self.gw, self.repo, self.tm)
        self._handle_reconcile(report, "startup")
        await self.refresh_account()
        self._ready_flag = True
        self._refresh_status()

    def _handle_reconcile(self, report, ctx: str) -> None:
        for a in report.actions:
            self.repo.system("INFO", "reconciliation_action", a, ctx=ctx)
        if report.clean:
            if "reconciliation" in self.holds:
                self.holds.discard("reconciliation")
                self._persist_holds()
        else:
            self.holds.add("reconciliation")
            self._persist_holds()
            self.repo.system("ERROR", "reconciliation_required", "; ".join(report.issues), ctx=ctx)
            self.notifier.notify("RECONCILIATION", "Reconciliation gerekli: " + "; ".join(report.issues)[:900])
        self._refresh_status()

    async def refresh_universe(self, initial: bool = False) -> None:
        selected = await build_universe(self.rest, self.public, self.s, self.filters)
        symbols = [s for s, _ in selected]
        managed = {p.symbol for p in self.repo.open_positions()}
        symbols += [m for m in managed if m not in symbols and m in self.filters]  # açık pozisyon verisi kesilmesin
        self.repo.upsert_symbols({s: self.filters[s] for s in symbols}, dict(selected))
        changed = set(symbols) != set(self.universe)
        self.universe = symbols
        self.repo.system("INFO", "universe", f"{len(symbols)} sembol", symbols=symbols)
        if changed and not initial:
            self.ws_market.restart()

    async def backfill(self, symbols: list[str]) -> None:
        for sym in symbols:
            for tf in TFS:
                rows = await self.rest.klines(sym, tf, limit=499)
                self.candles.set_history(sym, tf, [Candle.from_rest(r) for r in rows], self.rest.time.timestamp())
                await asyncio.sleep(0)  # WebSocket okumasını bekletme

    def _needs_backfill(self, sym: str) -> bool:
        now = now_ms()
        return any(len(self.candles.get(sym, tf)) < 300 or self.candles.has_gap(sym, tf)
                   or self.candles.is_stale(sym, tf, now, self.s.market_data_stale_factor) for tf in TFS)

    async def _backfill_background(self) -> None:
        """Bağlantı kurulunca eksik/bayat verisi olan sembolleri ARKA PLANDA doldurur. WebSocket bu sırada
        okunmaya devam eder (eskiden backfill bitene kadar okunmuyordu ve Binance bağlantıyı kesiyordu)."""
        try:
            todo = [s for s in list(self.universe) if self._needs_backfill(s)]
            if todo:
                await self.backfill(todo)
                self.repo.system("INFO", "backfill_done", f"{len(todo)} sembol")
        except Exception as exc:
            self.repo.system("WARNING", "backfill_failed", repr(exc))

    # ============================================================ websocket
    async def _market_url(self) -> str:
        v = pick_variant(self._url_variant["market"], self.ws_market.consecutive_failures, 2)
        self._url_in_use["market"] = v
        return market_stream_url(self.s.binance_ws_market_url, self.universe, TFS, variant=v)

    async def _user_url(self) -> str:
        key = await self.rest.create_listen_key()
        v = pick_variant(self._url_variant["user"], self.ws_user.consecutive_failures, 3)
        self._url_in_use["user"] = v
        return user_stream_url(self.s.binance_ws_user_url, key, variant=v)

    async def _on_market_connected(self) -> None:
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        self._backfill_task = asyncio.create_task(self._backfill_background())

    async def _on_user_connected(self) -> None:
        if self.ws_user.reconnects > 0:
            report = await reconcile(self.gw, self.repo, self.tm)
            self._handle_reconcile(report, "ws_reconnect")

    async def _on_ws_state(self, name: str, state: WSState) -> None:
        ws = self.ws_market if name == "market" else self.ws_user
        if state == WSState.CONNECTED:
            if self._url_variant.get(name) != self._url_in_use.get(name):
                self.repo.system("INFO", "ws_url_selected", f"{name}: biçim {self._url_in_use[name]}")
            self._url_variant[name] = self._url_in_use.get(name, 0)
            if self._ws_was_connected.get(name) is False and not self._throttled(f"ws_up:{name}", 1800):
                self.notifier.notify("WS_RECONNECT", f"WebSocket ({name}) yeniden bağlandı")
            self._ws_was_connected[name] = True
        elif state == WSState.DISCONNECTED and self._ws_was_connected.get(name):
            self._ws_was_connected[name] = False
            self.repo.system("WARNING", "ws_disconnect", name, error=ws.last_error)
            if not self._throttled(f"ws_down:{name}", 1800):  # kısa kopmalarda Telegram'ı boğma
                self.notifier.notify("WS_DISCONNECT", f"WebSocket ({name}) bağlantısı koptu; yeniden bağlanıyor")
        elif state == WSState.DISCONNECTED and ws.consecutive_failures in (3, 20):
            self.repo.system("ERROR", "ws_connect_failed", f"{name} bağlanamıyor", error=ws.last_error,
                             failures=ws.consecutive_failures)
            if not self._throttled(f"ws_fail:{name}", 3600):
                self.notifier.notify("WS_DISCONNECT", f"WebSocket ({name}) bağlanamıyor: {ws.last_error}")

    async def _on_market(self, msg: dict) -> None:
        data = msg.get("data", msg)
        if isinstance(data, list):  # !markPrice@arr
            for m in data:
                self.marks[m["s"]] = float(m["p"])
                if m.get("r") not in (None, ""):
                    self.funding[m["s"]] = float(m["r"])
            return
        if data.get("e") == "kline":
            k = data["k"]
            if not k.get("x"):
                return  # açık mum: strateji katmanına girmez
            sym, tf = data["s"], k["i"]
            self.candles.add_closed(sym, tf, Candle.from_ws(k))
            if tf == "3m":
                self._scan_queue.put_nowait(int(k["T"]))

    async def _on_user(self, msg: dict) -> None:
        et = msg.get("e")
        if et in ("ORDER_TRADE_UPDATE", "ALGO_UPDATE", "ACCOUNT_UPDATE"):
            self._manage_now.set()
        elif et == "listenKeyExpired":
            self.ws_user.restart()

    # ================================================================ loops
    async def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            db_ok = self.db.ping()
            if not db_ok:
                self.safe_reasons.add("db")
            else:
                self.safe_reasons.discard("db")
                now = now_ms()
                if self.ws_market.ready and self.universe:
                    stale = [s for s in self.universe if self.candles.is_stale(s, "3m", now, self.s.market_data_stale_factor)]
                    if len(stale) > len(self.universe) / 2:
                        await self.enter_safe_mode("market_data")
                    else:
                        self._clear_safe("market_data")
                try:
                    self._refresh_status()
                    self.repo.update_state(last_heartbeat_at=utc_now(), ws_market_connected=self.ws_market.ready,
                                           ws_user_connected=self.ws_user.ready,
                                           rest_ok=self.rest.consecutive_failures < 5 and self.rest.time.ok,
                                           clock_offset_ms=self.rest.time.offset_ms,
                                           safe_mode_reasons=sorted(self.safe_reasons))
                    if self.heartbeat_hook is not None:
                        self.heartbeat_hook({"state": self.status.value, "universe": len(self.universe),
                                             "ws": self.ws_market.ready and self.ws_user.ready,
                                             "open_positions": len(self.repo.open_positions())})
                except Exception:
                    log.exception("heartbeat write failed")
            await asyncio.sleep(self.s.heartbeat_interval_s)

    async def time_sync_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.time_sync_interval_s)
            try:
                off = await self.rest.sync_time()
                if abs(off) > 60_000:
                    await self.enter_safe_mode("clock")
                else:
                    self._clear_safe("clock")
            except Exception as exc:
                log.warning("time sync failed: %r", exc)
                await self.enter_safe_mode("clock")

    async def account_loop(self) -> None:
        last_snap = 0.0
        while not self._stop.is_set():
            await self.refresh_account()
            if time.monotonic() - last_snap > 60 and self.account is not None:
                last_snap = time.monotonic()
                pf = self._portfolio()
                self.repo.pnl_snapshot(equity=pf.equity, available_balance=pf.available_balance,
                                       unrealized_pnl=self.unrealized_bot, margin_used=self.repo.state().margin_used or 0,
                                       open_risk=pf.total_open_risk, daily_pnl=pf.equity - pf.day_start_equity)
            await asyncio.sleep(15)

    async def manage_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._manage_now.wait(), timeout=self.s.position_sync_interval_s)
            except asyncio.TimeoutError:
                pass
            self._manage_now.clear()
            try:
                await self.tm.manage_all()
            except Exception as exc:
                self.repo.system("ERROR", "manage_loop", repr(exc))

    async def command_loop(self) -> None:
        while not self._stop.is_set():
            for cmd in self.repo.pending_commands():
                try:
                    result = await self.handle_command(cmd.command, cmd.requested_by)
                    self.repo.complete_command(cmd.id, "DONE", result)
                except Exception as exc:
                    self.repo.complete_command(cmd.id, "FAILED", repr(exc))
                    self.repo.system("ERROR", "command_failed", repr(exc), command=cmd.command)
            await asyncio.sleep(2)

    async def handle_command(self, command: str, actor: str) -> str:
        if command == "KILL":
            return await self.kill_switch(actor)
        if command == "RESUME":
            if "expired" in self.holds:
                return "trial bitti; bot yeniden başlatılamaz"
            cleared = [h for h in self.holds if h != "expired"]
            self.holds = {h for h in self.holds if h == "expired"}
            self._persist_holds()
            report = await reconcile(self.gw, self.repo, self.tm)
            self._handle_reconcile(report, "resume")
            self.repo.audit(actor, "resume", cleared=cleared, reconcile_clean=report.clean)
            return f"cleared={cleared} reconcile_clean={report.clean}"
        return "unknown command"

    async def kill_switch(self, actor: str) -> str:
        self.holds.add("kill")
        self._persist_holds()
        self._refresh_status()  # yeni sinyal / emir anında durur
        self.repo.audit(actor, "kill_switch_start")
        self.notifier.notify("KILL_SWITCH", f"Kill switch tetiklendi ({actor}). Tüm emirler iptal, pozisyonlar kapatılıyor")
        res = await flatten_all(self.gw, self.repo, self.tm, "KILL_SWITCH")
        self.repo.audit(actor, "kill_switch_done", ok=res.ok, remaining_positions=res.remaining_positions,
                        remaining_orders=res.remaining_orders, log=res.log)
        msg = "doğrulandı: 0 pozisyon, 0 açık emir" if res.ok else \
            f"DOĞRULANAMADI pozisyon={res.remaining_positions} emir={res.remaining_orders}"
        self.notifier.notify("KILL_SWITCH", f"Kill switch tamamlandı — {msg}. State=EMERGENCY_STOP")
        return msg

    async def expire(self) -> None:
        self.holds.add("expired")
        self._persist_holds()
        self._refresh_status()
        res = await flatten_all(self.gw, self.repo, self.tm, "TRIAL_EXPIRED")
        with self.db.session() as sess:
            row = sess.get(TrialState, 1)
            if row and row.expired_at is None:
                row.expired_at = utc_now()
        self.repo.audit("worker", "trial_expired", ok=res.ok, remaining=res.remaining_positions + res.remaining_orders)
        self.notifier.notify("TRIAL_EXPIRED", "72 saatlik trial sona erdi. Pozisyonlar kapatıldı "
                                              f"({'doğrulandı' if res.ok else 'DOĞRULANAMADI'}). Bot EXPIRED.")

    async def trial_loop(self) -> None:
        while not self._stop.is_set():
            if "expired" not in self.holds and self.trial_expired():
                await self.expire()
            await asyncio.sleep(30)

    async def context_loop(self) -> None:
        while not self._stop.is_set():
            ts = now_ms()
            for sym in list(self.universe):
                try:
                    oi = float((await self.rest.open_interest(sym))["openInterest"])
                    px = self.marks.get(sym) or (self.candles.get(sym, "3m", 1) or [None])[-1]
                    price = px if isinstance(px, float) else (px.close if px else 0.0)
                    if price:
                        self.oi.add(sym, ts, oi, price)
                except Exception as exc:
                    log.debug("OI %s: %r", sym, exc)
            if self.fng:
                await self.fng.get()
            try:
                with self.db.session() as sess:
                    for sym in self.universe:
                        last = (self.oi.samples.get(sym) or [None])[-1]
                        sess.add(MarketSnapshot(symbol=sym, mark_price=self.marks.get(sym),
                                                open_interest=last.oi if last else None,
                                                funding_rate=self.funding.get(sym),
                                                fear_greed=self.fng.value if self.fng else None))
            except Exception:
                log.exception("market snapshot yazılamadı")
            await asyncio.sleep(300)

    async def keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30 * 60)
            try:
                await self.rest.keepalive_listen_key()
            except Exception as exc:
                log.warning("listenKey keepalive failed: %r", exc)
                self.ws_user.restart()

    async def universe_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.universe_refresh_minutes * 60)
            try:
                await self.refresh_universe()
            except Exception as exc:
                self.repo.system("WARNING", "universe_refresh_failed", repr(exc))

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            close_t = await self._scan_queue.get()
            if close_t <= self._last_scanned:
                continue
            await asyncio.sleep(2.0)  # aynı 3m kapanışına ait diğer sembollerin mumlarını bekle
            while not self._scan_queue.empty():
                close_t = max(close_t, self._scan_queue.get_nowait())
            self._last_scanned = close_t
            try:
                await self.scan(close_t)
            except Exception as exc:
                self.repo.system("ERROR", "scan_failed", repr(exc))

    # ================================================================= scan
    async def scan(self, close_t: int) -> None:
        if not self.gate().can_trade and self.status in (BotStatus.EXPIRED, BotStatus.EMERGENCY_STOP):
            return  # piyasa taraması dahi gereksiz
        open_syms = {p.symbol for p in self.repo.open_positions()}
        fg = self.fng.value if self.fng else None
        for sym in list(self.universe):
            if sym in open_syms or sym not in self.filters or sym in self.foreign_symbols:
                continue
            if any(self.candles.has_gap(sym, tf) or self.candles.is_stale(sym, tf, now_ms(), self.s.market_data_stale_factor)
                   for tf in TFS):
                continue
            c3 = self.candles.get(sym, "3m", 300)
            if not c3 or c3[-1].close_time + 1 < close_t - TIMEFRAME_MS["3m"]:
                continue
            ctx = MarketContext(oi_change=self.oi.change(sym), funding_rate=self.funding.get(sym), fear_greed=fg)
            for ev in evaluate_symbol(sym, self.candles.get(sym, "15m", 300), self.candles.get(sym, "5m", 400), c3,
                                      ctx, self.s):
                if not ev.structural:
                    continue
                await self._handle_signal(ev, ctx)
                open_syms = {p.symbol for p in self.repo.open_positions()}
                if sym in open_syms:
                    break

    async def _handle_signal(self, ev, ctx: MarketContext) -> None:
        reason = ";".join(ev.reasons) or None
        sig_id = self.repo.add_signal(symbol=ev.symbol, side=ev.side.value, candle_time=ev.candle_time, score=ev.score,
                                      accepted=False, reject_reason=reason, setup=ev.setup, snapshot=ev.snapshot,
                                      components=components_as_dicts(ev))
        if sig_id is None or ev.setup is None:
            return
        if ev.symbol in self.foreign_symbols:
            self.repo.update_signal(sig_id, reject_reason="foreign_position_on_symbol")
            return
        gate = self.gate()
        if not gate.can_trade:
            self.repo.update_signal(sig_id, reject_reason=f"gate:{gate.reason}")
            return
        try:
            spread, depth = book_metrics(await self.rest.depth(ev.symbol, 50))
        except Exception as exc:
            self.repo.update_signal(sig_id, reject_reason=f"depth_unavailable:{exc!r}")
            return
        ctx.spread_pct, ctx.depth_usdt = spread, depth
        ev.snapshot["spread"] = spread
        ev.snapshot["depth_usdt"] = depth
        with self.db.session() as sess:
            sess.add(MarketSnapshot(symbol=ev.symbol, mark_price=self.marks.get(ev.symbol), spread_pct=spread,
                                    depth_usdt=depth, funding_rate=ctx.funding_rate, fear_greed=ctx.fear_greed))
        await self.refresh_account()
        pf = self._portfolio()
        closes = self.candles.closes(ev.symbol, "5m", self.s.correlation_lookback + 1)
        corrs = {p.symbol: correlation(closes, self.candles.closes(p.symbol, "5m", self.s.correlation_lookback + 1))
                 for p in pf.open_positions}
        decision = evaluate_trade(Candidate(ev.setup, spread, depth, corrs), pf, self.gate(),
                                  self.filters[ev.symbol], self.s)
        ev.snapshot["risk"] = {"allowed": decision.allowed, "reasons": decision.reasons,
                               "risk_factor": decision.risk_factor, "planned_rr": decision.planned_rr,
                               "qty": getattr(decision.sizing, "qty", None),
                               "leverage": getattr(decision.sizing, "leverage", None),
                               "risk_actual": getattr(decision.sizing, "risk_actual", None),
                               "liq_price": getattr(decision.sizing, "liq_price", None)}
        if not decision.allowed:
            r = decision.reasons[0] if decision.reasons else "rejected"
            self.repo.update_signal(sig_id, reject_reason=f"risk:{r}", snapshot=ev.snapshot)
            self.repo.risk_event("trade_rejected", ev.symbol, reasons=decision.reasons, score=ev.score)
            key = r.split(":")[0]
            alert = {"spread": "SPREAD", "sizing": "LIQ_BUFFER" if "liquidation" in r else None,
                     "total_open_risk": "MAX_RISK"}.get(key)
            if alert and not self._throttled(f"{alert}:{ev.symbol}"):
                self.notifier.notify(alert, f"{ev.symbol} {ev.side.value} sinyali reddedildi: {r}")
            return
        self.repo.update_signal(sig_id, accepted=True, reject_reason=None, snapshot=ev.snapshot)
        ev.setup.stop = decision.stop
        await self.tm.open_position(ev.setup, decision, sig_id)
        await self.refresh_account()

    # ================================================================== run
    async def run(self) -> None:
        tg = asyncio.create_task(self.notifier.run())
        while True:
            try:
                await self.bootstrap()
                break
            except Exception as exc:
                self.repo.system("ERROR", "bootstrap_failed", repr(exc))
                if not self._throttled("bootstrap_failed", 900):
                    self.notifier.notify("API_ERROR", f"Başlatma hatası (Binance bağlantısı / API key?): {exc!r}. "
                                                      "30 sn'de bir tekrar deneniyor; trial henüz başlamadı ise başlamaz.")
                await self.enter_safe_mode("rest")
                await asyncio.sleep(30)
        self._clear_safe("rest")
        tasks = [tg, asyncio.create_task(self.heartbeat_loop()), asyncio.create_task(self.command_loop())]
        if "expired" not in self.holds:
            tasks += [asyncio.create_task(c) for c in (
                self.ws_market.run(), self.ws_user.run(), self.time_sync_loop(), self.account_loop(),
                self.manage_loop(), self.scan_loop(), self.context_loop(), self.keepalive_loop(),
                self.universe_loop(), self.trial_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.rest.close()
        await self.public.close()

    def request_stop(self) -> None:
        self._stop.set()


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = TradingWorker(s)
    loop = asyncio.new_event_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:  # pragma: no cover
            pass
    loop.run_until_complete(worker.run())


if __name__ == "__main__":
    main()
