"""Backtest motoru.

Live ile AYNI Strategy Engine (evaluate_symbol), AYNI Risk Engine (evaluate_trade), AYNI Position Sizing,
AYNI TP planı ve AYNI breakeven mantığı (stop_after_tp) kullanılır.

Simülasyon: commission, slippage, partial TP, partial fill, BE, risk, leverage, max position,
max toplam risk, günlük zarar, ardışık SL cooldown ve funding.

Look-ahead yok:
- Sinyal yalnızca t anında KAPANMIŞ 3m/5m/15m mumlarla üretilir (close_time <= t).
- Entry bir sonraki 3m mumun açılışında gerçekleşir.
- Aynı mum içinde SL ve TP'nin ikisine de dokunulduysa önce SL varsayılır (kötümser).
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.smc.config import Settings
from backend.smc.core.types import Candle, Side
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.engine import Candidate, OpenPositionView, PortfolioState, TradingGate, evaluate_trade, \
    register_trade_result
from backend.smc.risk.plan import TPLevel, open_risk, plan_take_profits, r_multiple, stop_after_tp
from backend.smc.risk.sizing import per_unit_loss, round_to_step
from backend.smc.backtest.metrics import BTTrade, compute_metrics
from backend.smc.strategy.signal import MarketContext, evaluate_symbol

FUNDING_HOURS = (0, 8, 16)


def default_filters(symbol: str, price: float) -> SymbolFilters:
    """Exchange info yoksa fiyat ölçeğine göre makul filtreler."""
    tick = Decimal("0.1") if price > 1000 else Decimal("0.01") if price > 10 else Decimal("0.0001")
    step = Decimal("0.001") if price > 1000 else Decimal("0.01") if price > 10 else Decimal("1")
    return SymbolFilters(symbol, tick, step, step, Decimal("100000000"), step, Decimal("100000000"), 5.0, 20, 0.004, 0.0)


@dataclass
class BTPosition:
    symbol: str
    side: Side
    entry: float
    stop: float
    initial_stop: float
    qty: float
    remaining: float
    levels: list[TPLevel]
    risk_amount: float
    leverage: int
    opened_t: int
    score: int
    partial_fill: bool
    gross: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0
    tp_hits: list[str] = field(default_factory=list)
    be_active: bool = False
    exit_value: float = 0.0  # sum(price*qty) çıkışlar
    last_funding_t: int = 0


@dataclass
class BacktestResult:
    trades: list[BTTrade]
    equity_curve: list[tuple[int, float]]
    metrics: dict
    signals: int


def _pre_trigger(c3: list[Candle]) -> bool:
    """3m final trigger'ın gerekli koşulu (her iki yön için). Hız optimizasyonu; sonucu değiştirmez."""
    if len(c3) < 3:
        return False
    last, p1, p2 = c3[-1], c3[-2], c3[-3]
    long_ok = last.bullish and last.close > max(p1.high, p2.high) and last.delta > 0
    short_ok = last.bearish and last.close < min(p1.low, p2.low) and last.delta < 0
    return long_ok or short_ok


class Backtester:
    def __init__(self, s: Settings, data: dict[str, dict[str, list[Candle]]],
                 filters: dict[str, SymbolFilters] | None = None, *, trade_start: int | None = None,
                 trade_end: int | None = None, spread_pct: float = 0.0002, depth_usdt: float = 1e9,
                 funding_rates: dict[str, list[tuple[int, float]]] | None = None):
        self.s = s
        self.data = data
        self.filters = filters or {}
        self.trade_start = trade_start
        self.trade_end = trade_end
        self.spread = spread_pct
        self.depth = depth_usdt
        self.funding_rates = funding_rates or {}
        self.rng = random.Random(s.bt_seed)
        self.realized = 0.0
        self.positions: dict[str, BTPosition] = {}
        self.pending: dict[str, tuple] = {}
        self.trades: list[BTTrade] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.day_key: str | None = None
        self.day_start_equity = s.starting_equity
        self.consec = 0
        self.cooldown_until: datetime | None = None
        self.signal_count = 0
        self._close_times = {sym: {tf: [c.close_time for c in cs] for tf, cs in tfs.items()} for sym, tfs in data.items()}

    # -------------------------------------------------------------- helpers
    def _filters(self, sym: str, price: float) -> SymbolFilters:
        if sym not in self.filters:
            self.filters[sym] = default_filters(sym, price)
        return self.filters[sym]

    def _upto(self, sym: str, tf: str, t: int, n: int) -> list[Candle]:
        idx = bisect.bisect_right(self._close_times[sym][tf], t)
        return self.data[sym][tf][max(0, idx - n): idx]

    def _equity(self, marks: dict[str, float]) -> float:
        unreal = sum((marks.get(p.symbol, p.entry) - p.entry) * p.side.sign * p.remaining for p in self.positions.values())
        return self.s.starting_equity + self.realized + unreal

    def _funding_rate(self, sym: str, t: int) -> float:
        hist = self.funding_rates.get(sym)
        if not hist:
            return self.s.bt_funding_rate_8h
        idx = bisect.bisect_right([h[0] for h in hist], t) - 1
        return hist[idx][1] if idx >= 0 else self.s.bt_funding_rate_8h

    # ---------------------------------------------------------------- core
    def run(self) -> BacktestResult:
        timeline = sorted({c.close_time for tfs in self.data.values() for c in tfs.get("3m", [])})
        idx3 = {sym: {c.close_time: i for i, c in enumerate(tfs["3m"])} for sym, tfs in self.data.items()}
        marks: dict[str, float] = {}
        for t in timeline:
            for sym, tfs in self.data.items():
                i = idx3[sym].get(t)
                if i is None:
                    continue
                bar = tfs["3m"][i]
                marks[sym] = bar.close
                if sym in self.pending:
                    self._fill_pending(sym, bar)
                if sym in self.positions:
                    self._manage(sym, bar)
            eq = self._equity(marks)
            day = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if day != self.day_key:
                self.day_key, self.day_start_equity = day, eq
            self.equity_curve.append((t, eq))
            if (self.trade_start and t < self.trade_start) or (self.trade_end and t > self.trade_end):
                continue
            for sym, tfs in self.data.items():
                i = idx3[sym].get(t)
                if i is None or sym in self.positions or sym in self.pending:
                    continue
                c3 = tfs["3m"][max(0, i - 299): i + 1]
                if not _pre_trigger(c3):
                    continue
                c5 = self._upto(sym, "5m", t, 300)
                c15 = self._upto(sym, "15m", t, 200)
                ctx = MarketContext(funding_rate=self._funding_rate(sym, t), spread_pct=self.spread,
                                    depth_usdt=self.depth)
                for ev in evaluate_symbol(sym, c15, c5, c3, ctx, self.s):
                    if ev.setup is None:
                        continue
                    self.signal_count += 1
                    self._try_open(ev, t, marks)
                    break
        # açık kalanları son fiyattan kapat
        for sym in list(self.positions):
            last = self.data[sym]["3m"][-1]
            self._close(sym, last.close, last.close_time, "END_OF_DATA")
        curve = [e for _, e in self.equity_curve]
        return BacktestResult(self.trades, self.equity_curve, compute_metrics(self.trades, curve, self.s.starting_equity),
                              self.signal_count)

    def _portfolio(self, t: int, marks: dict[str, float]) -> PortfolioState:
        eq = self._equity(marks)
        margin = sum(p.remaining * p.entry / p.leverage for p in self.positions.values())
        return PortfolioState(
            equity=eq, available_balance=max(0.0, eq - margin), day_start_equity=self.day_start_equity,
            open_positions=[OpenPositionView(p.symbol, p.side, open_risk(p.side, p.entry, p.stop, p.remaining, self.s))
                            for p in self.positions.values()],
            consecutive_losses=self.consec, cooldown_until=self.cooldown_until,
            now=datetime.fromtimestamp(t / 1000, tz=timezone.utc))

    def _try_open(self, ev, t: int, marks: dict[str, float]) -> None:
        setup = ev.setup
        f = self._filters(setup.symbol, setup.entry)
        from backend.smc.risk.correlation import correlation
        closes = [c.close for c in self._upto(setup.symbol, "5m", t, self.s.correlation_lookback + 1)]
        corrs = {p: correlation(closes, [c.close for c in self._upto(p, "5m", t, self.s.correlation_lookback + 1)])
                 for p in self.positions}
        decision = evaluate_trade(Candidate(setup, self.spread, self.depth, corrs), self._portfolio(t, marks),
                                  TradingGate(True), f, self.s)
        if decision.allowed:
            self.pending[setup.symbol] = (setup, decision, ev.score)

    def _fill_pending(self, sym: str, bar: Candle) -> None:
        setup, decision, score = self.pending.pop(sym)
        side = setup.side
        f = self._filters(sym, setup.entry)
        entry = bar.open * (1 + side.sign * self.s.slippage_pct)
        limit = setup.entry * (1 + side.sign * self.s.entry_limit_offset_pct)
        if (entry - limit) * side.sign > 0:  # limit fiyatın ötesinde açıldı → dolmaz
            return
        stop = decision.stop
        if (entry - stop) * side.sign <= 0:
            return
        qty = decision.sizing.qty
        partial = False
        if self.rng.random() < self.s.bt_partial_fill_prob:
            qty = round_to_step(qty * self.rng.uniform(0.3, 0.9), f.step_size, "down")
            partial = True
            if qty < float(f.min_qty):
                return
        levels = plan_take_profits(side, entry, stop, qty, f, self.s)
        pul = per_unit_loss(side, entry, stop, self.s)[0]
        fee = entry * qty * self.s.taker_fee
        self.realized -= fee
        self.positions[sym] = BTPosition(sym, side, entry, stop, stop, qty, qty, levels, qty * pul,
                                         decision.sizing.leverage, bar.open_time, score, partial, fees=fee,
                                         slippage=abs(entry - bar.open) * qty, last_funding_t=bar.open_time)

    def _apply_funding(self, p: BTPosition, bar: Candle) -> None:
        start = datetime.fromtimestamp(p.last_funding_t / 1000, tz=timezone.utc)
        end = datetime.fromtimestamp(bar.close_time / 1000, tz=timezone.utc)
        cur = start.replace(minute=0, second=0, microsecond=0)
        while cur <= end:
            if cur.hour in FUNDING_HOURS and start < cur <= end:
                rate = self._funding_rate(p.symbol, int(cur.timestamp() * 1000))
                amt = -p.side.sign * rate * p.remaining * bar.close
                p.funding += amt
                self.realized += amt
            cur += timedelta(hours=1)
        p.last_funding_t = bar.close_time

    def _exit_part(self, p: BTPosition, price: float, qty: float, slip_px: float) -> None:
        fill = price - p.side.sign * slip_px
        pnl = (fill - p.entry) * p.side.sign * qty
        fee = fill * qty * self.s.taker_fee
        p.gross += pnl
        p.fees += fee
        p.slippage += slip_px * qty
        p.exit_value += fill * qty
        p.remaining = round(p.remaining - qty, 12)
        self.realized += pnl - fee

    def _manage(self, sym: str, bar: Candle) -> None:
        p = self.positions[sym]
        if bar.open_time < p.opened_t:
            return
        self._apply_funding(p, bar)
        side = p.side
        hit_stop = (bar.low <= p.stop) if side is Side.LONG else (bar.high >= p.stop)
        if hit_stop:  # kötümser: önce stop
            reason = "BREAKEVEN_STOP" if p.be_active else "SL"
            gap_px = min(bar.open, p.stop) if side is Side.LONG else max(bar.open, p.stop)
            self._exit_part(p, gap_px, p.remaining, gap_px * self.s.slippage_pct)
            self._finish(sym, bar.close_time, reason)
            return
        f = self.filters[sym]
        for lvl in p.levels:
            if lvl.filled:
                continue
            reached = (bar.high >= lvl.price) if side is Side.LONG else (bar.low <= lvl.price)
            if not reached:
                break
            q = min(lvl.qty, p.remaining)
            self._exit_part(p, lvl.price, q, lvl.price * self.s.slippage_pct)
            lvl.filled = True
            p.tp_hits.append(lvl.name)
            if p.remaining <= 1e-12:
                self._finish(sym, bar.close_time, "TP3")  # tüm TP dilimleri doldu
                return
            new_stop = stop_after_tp(side, p.entry, p.levels, set(p.tp_hits), p.stop, self.s, f.tick_size)
            if new_stop != p.stop:
                p.stop = new_stop
                p.be_active = True
                # Aynı mumda yeni stop'a da dokunulduysa kötümser varsay: stop sonradan tetiklendi
                if (bar.low <= p.stop) if side is Side.LONG else (bar.high >= p.stop):
                    self._exit_part(p, p.stop, p.remaining, p.stop * self.s.slippage_pct)
                    self._finish(sym, bar.close_time, "BREAKEVEN_STOP")
                    return

    def _close(self, sym: str, price: float, t: int, reason: str) -> None:
        p = self.positions[sym]
        self._exit_part(p, price, p.remaining, 0.0)
        self._finish(sym, t, reason)

    def _finish(self, sym: str, t: int, reason: str) -> None:
        p = self.positions.pop(sym)
        exited = p.qty - p.remaining
        net = p.gross - p.fees + p.funding
        trade = BTTrade(sym, p.side.value, p.opened_t, t, p.entry, p.exit_value / exited if exited else p.entry,
                        p.qty, p.risk_amount, p.gross, p.fees, p.funding, p.slippage, net, r_multiple(net, p.risk_amount),
                        reason, list(p.tp_hits), p.score, p.leverage, p.partial_fill)
        self.trades.append(trade)
        self.consec, cd = register_trade_result(self.consec, net, reason, datetime.fromtimestamp(t / 1000, tz=timezone.utc),
                                                self.s)
        if cd is not None:
            self.cooldown_until = cd


def split_periods(start: int, end: int, fractions: tuple[float, float, float] = (0.6, 0.2, 0.2)) -> dict[str, tuple[int, int]]:
    """Training / Validation / Out-of-Sample zaman dilimleri (sıralı, çakışmasız)."""
    total = end - start
    a = start + int(total * fractions[0])
    b = a + int(total * fractions[1])
    return {"training": (start, a), "validation": (a, b), "out_of_sample": (b, end)}
