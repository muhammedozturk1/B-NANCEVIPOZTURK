import random

import pytest

from backend.smc.backtest.engine import Backtester, BTPosition, split_periods
from backend.smc.backtest.metrics import BTTrade, compute_metrics, max_drawdown
from backend.smc.core.types import Candle, Side
from backend.smc.execution.state_machine import BotStatus
from backend.smc.risk.plan import TPLevel
from backend.smc.worker import TradingWorker
from tests.smc.conftest import make_settings, run
from tests.smc.fakes import FakeExchange


def synthetic(n_minutes=3 * 24 * 60, seed=7, start=100.0):
    rng = random.Random(seed)
    m1, p, t0 = [], start, 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    for i in range(n_minutes):
        o = p
        drift = 0.0004 * (1 if (i // 600) % 2 == 0 else -1)
        p = max(1, p * (1 + drift + rng.gauss(0, 0.0015)))
        v = rng.uniform(50, 150)
        m1.append(Candle(t0 + i * 60_000, o, max(o, p) * 1.0004, min(o, p) * 0.9996, p, v, t0 + (i + 1) * 60_000 - 1,
                         v * (0.6 if p > o else 0.4)))

    def agg(k):
        out = []
        for j in range(0, len(m1) - k + 1, k):
            g = m1[j:j + k]
            out.append(Candle(g[0].open_time, g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close,
                              sum(c.volume for c in g), g[-1].close_time, sum(c.taker_buy_volume for c in g)))
        return out
    return {"3m": agg(3), "5m": agg(5), "15m": agg(15)}


def test_backtest_runs_with_same_engines_and_reports_metrics():
    s = make_settings()
    data = {"AAAUSDT": synthetic(seed=1), "BBBUSDT": synthetic(seed=2, start=20)}
    res = Backtester(s, data).run()
    m = res.metrics
    for k in ("total_trades", "win_rate", "net_pnl", "profit_factor", "average_r", "max_drawdown",
              "max_consecutive_loss", "average_trade_duration_min", "tp1_hit_rate", "tp2_hit_rate", "tp3_hit_rate",
              "sl_rate"):
        assert k in m
    assert len(res.equity_curve) > 0
    for t in res.trades:  # aynı risk motoru: işlem başına risk ≤ %5 equity (+ partial fill küçültür)
        assert t.risk_amount <= s.starting_equity * 3 * s.risk_per_trade
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.commission + t.funding)


def test_no_look_ahead_in_higher_timeframes():
    s = make_settings()
    data = {"AAAUSDT": synthetic(n_minutes=600)}
    bt = Backtester(s, data)
    t = data["AAAUSDT"]["3m"][50].close_time
    assert all(c.close_time <= t for c in bt._upto("AAAUSDT", "5m", t, 500))
    assert all(c.close_time <= t for c in bt._upto("AAAUSDT", "15m", t, 500))


def _bt_with_position(side=Side.LONG):
    s = make_settings(taker_fee=0, slippage_pct=0)
    data = {"X": {"3m": [], "5m": [], "15m": []}}
    bt = Backtester(s, data)
    from decimal import Decimal
    from backend.smc.market_data.universe import SymbolFilters
    bt.filters["X"] = SymbolFilters("X", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("1e6"),
                                    Decimal("0.001"), Decimal("1e6"), 5)
    lv = [TPLevel("TP1", 1, 101, 2), TPLevel("TP2", 2, 102, 1), TPLevel("TP3", 3, 103, 1)]
    bt.positions["X"] = BTPosition("X", side, 100, 99, 99, 4, 4, lv, 4, 10, 0, 10, False, last_funding_t=0)
    return bt


def test_backtest_pessimistic_sl_first_when_both_touched():
    bt = _bt_with_position()
    bt._manage("X", Candle(1, 100, 101.5, 98.5, 100, 1, 2))
    assert bt.trades[-1].exit_reason == "SL" and bt.trades[-1].net_pnl == pytest.approx(-4)


def test_backtest_partial_tp_and_breakeven():
    bt = _bt_with_position()
    bt._manage("X", Candle(1, 100, 101.2, 100.1, 101, 1, 2))  # TP1 (low BE üstünde)
    p = bt.positions["X"]
    assert p.tp_hits == ["TP1"] and p.remaining == 2 and p.stop == pytest.approx(100.0) and p.be_active
    bt._manage("X", Candle(3, 101, 101.1, 99.8, 100, 1, 4))  # BE stop
    t = bt.trades[-1]
    assert t.exit_reason == "BREAKEVEN_STOP" and t.net_pnl == pytest.approx(2.0)


def test_metrics_and_splits():
    tr = [BTTrade("X", "LONG", 0, 60_000, 1, 1, 1, 1, 2, 0, 0, 0, 2, 2, "TP3", ["TP1", "TP2", "TP3"], 10, 10, False),
          BTTrade("X", "LONG", 0, 120_000, 1, 1, 1, 1, -1, 0, 0, 0, -1, -1, "SL", [], 10, 10, False)]
    m = compute_metrics(tr, [102, 101], 100)
    assert m["win_rate"] == 0.5 and m["profit_factor"] == 2 and m["tp1_hit_rate"] == 0.5 and m["sl_rate"] == 0.5
    assert max_drawdown([100, 110, 99, 120]) == (11, pytest.approx(0.1))
    sp = split_periods(0, 1000)
    assert sp["training"] == (0, 600) and sp["validation"] == (600, 800) and sp["out_of_sample"] == (800, 1000)


# ------------------------------------------------------------------ worker
def make_worker(db):
    w = TradingWorker(make_settings(entry_order_timeout_s=0.05), db=db)
    w.gw = FakeExchange()
    w.tm.gw = w.gw
    w.notifier.notify = lambda kind, text: w._sent.append(kind)
    w._sent = []
    from backend.smc.core.trial import ensure_trial
    from backend.smc.core.types import utc_now
    with db.session() as s:
        ensure_trial(s, utc_now(), 72)
    w._ready_flag = True
    return w


def test_worker_gate_requires_websocket_and_status(db):
    w = make_worker(db)
    w.rest.time.ok = True
    w._refresh_status()
    assert w.status == BotStatus.RUNNING
    g = w.gate()
    assert not g.can_trade and g.reason == "websocket_not_ready"


def test_worker_kill_switch_and_resume(db):
    w = make_worker(db)
    w.gw.open_position("BTCUSDT", 0.01, 50000)
    msg = run(w.handle_command("KILL", "admin"))
    assert "0 pozisyon" in msg and not w.gw.positions_
    assert w.status == BotStatus.EMERGENCY_STOP and w.repo.state().state == "EMERGENCY_STOP"
    assert "kill" in w.repo.state().manual_hold_reasons  # restart sonrası da korunur
    assert not w.gate().can_trade
    run(w.handle_command("RESUME", "admin"))
    assert w.status == BotStatus.RUNNING and "KILL_SWITCH" in w._sent


def test_worker_expire_flattens_and_blocks_resume(db):
    w = make_worker(db)
    w.gw.open_position("ETHUSDT", 1, 3000)
    run(w.expire())
    assert w.status == BotStatus.EXPIRED and not w.gw.positions_
    assert "trial bitti" in run(w.handle_command("RESUME", "admin"))
    assert w.status == BotStatus.EXPIRED and "TRIAL_EXPIRED" in w._sent


def test_worker_virtual_equity_starts_at_100(db):
    w = make_worker(db)
    w.gw.equity = 15000  # testnet bakiyesi büyük olsa da
    run(w.refresh_account())
    pf = w._portfolio()
    assert pf.equity == pytest.approx(100.0) and pf.available_balance == pytest.approx(100.0)
    assert w.repo.state().day_start_equity == pytest.approx(100.0)


def test_no_market_data_safe_mode_while_warming_up(db):
    """Açılışta geçmiş veri arka planda inerken 'veri bayat' sanıp SAFE_MODE'a geçmemeli."""
    import asyncio
    import time as _t
    from backend.smc.market_data.ws import WSState
    w = make_worker(db)
    w.universe = ["AAAUSDT", "BBBUSDT"]
    w.ws_market.state = WSState.CONNECTED
    w._market_connected_at = _t.monotonic()

    async def one_beat():
        task = asyncio.create_task(w.heartbeat_loop())
        await asyncio.sleep(0.2)
        w.request_stop()
        task.cancel()
    run(one_beat())
    assert "market_data" not in w.safe_reasons
    w._market_connected_at = _t.monotonic() - 600  # ısınma bitti, veri hâlâ yok -> SAFE_MODE
    w._stop.clear()
    run(one_beat())
    assert "market_data" in w.safe_reasons
