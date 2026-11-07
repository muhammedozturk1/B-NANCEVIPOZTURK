"""SMC Scalper yeni stratejisi: 4s kanal kırılımı, stop = kasanın %1'i, tek hedef = kasanın %2'si (net)."""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from backend.smc.core.types import Side
from backend.smc.database.models import SystemEvent, TrialState
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.plan import plan_take_profits, planned_rr
from backend.smc.risk.sizing import compute_position_size, per_unit_loss
from backend.smc.strategy.breakout import evaluate_breakout
from tests.smc.conftest import from_closes, make_settings, run

H4 = 14_400_000


def bo_settings(**kw):
    return make_settings(strategy="breakout", **kw)


def channel_then(last: float, n: int = 60) -> list[float]:
    return [100 + 0.3 * math.sin(i / 2) for i in range(n)] + [last]


def test_settings_force_one_percent_risk_and_single_target():
    s = bo_settings(risk_per_trade=0.05)  # eski SMC_RISK_PER_TRADE=0.05 env'i burada etkisiz
    assert s.risk_per_trade == 0.01
    assert (s.tp1_pct, s.tp2_pct, s.tp3_pct) == (1.0, 0.0, 0.0)
    assert not s.trial_enabled


def test_long_breakout_and_no_signal_inside_channel():
    s = bo_settings()
    evs = evaluate_breakout("TESTUSDT", from_closes(channel_then(104.0), tf_ms=H4), 50, s)
    assert len(evs) == 1 and evs[0].side is Side.LONG and evs[0].setup is not None
    st = evs[0].setup
    assert st.entry == pytest.approx(104.0) and st.stop < st.entry
    assert evs[0].components[0].details["text"].startswith("4s mum son 20 mumun tepesini")
    assert evaluate_breakout("TESTUSDT", from_closes(channel_then(100.1), tf_ms=H4), 50, s) == []


def test_short_breakdown_and_fear_greed_block():
    s = bo_settings()
    evs = evaluate_breakout("TESTUSDT", from_closes(channel_then(96.0), tf_ms=H4), 50, s)
    assert [e.side for e in evs] == [Side.SHORT] and evs[0].setup.stop > evs[0].setup.entry
    blocked = evaluate_breakout("TESTUSDT", from_closes(channel_then(96.0), tf_ms=H4), 20, s)
    assert blocked[0].setup is None and blocked[0].reasons[0].startswith("fng:")
    blocked = evaluate_breakout("TESTUSDT", from_closes(channel_then(104.0), tf_ms=H4), 80, s)
    assert blocked[0].setup is None


@pytest.mark.parametrize("side,entry,stop", [(Side.LONG, 100.0, 92.0), (Side.SHORT, 100.0, 108.0),
                                             (Side.LONG, 2.5, 2.4)])
def test_target_is_exactly_two_r_net_and_single_level(filters, side, entry, stop):
    s = bo_settings()
    levels = plan_take_profits(side, entry, stop, 1.0, filters, s)
    assert [l.name for l in levels] == ["TP1"] and levels[0].qty == pytest.approx(1.0)
    exact = plan_take_profits(side, entry, stop, 1.0,
                              SymbolFilters("X", Decimal("0.0000001"), Decimal("0.001"), Decimal("0.001"),
                                            Decimal("10000"), Decimal("0.001"), Decimal("10000"), 5.0, 20, 0.004, 0.0), s)
    assert planned_rr(side, entry, stop, exact, s) == pytest.approx(2.0, rel=1e-6)


def test_sizing_risks_one_percent_of_100_usdt_with_wide_stop(filters):
    s = bo_settings()
    z = compute_position_size(side=Side.LONG, equity=100.0, available_balance=100.0, entry=100.0, stop=92.0,
                              filters=filters, cfg=s)
    assert z.ok and z.risk_actual == pytest.approx(1.0, rel=0.02) and z.risk_actual <= 1.0 + 1e-9
    assert z.risk_actual == pytest.approx(z.qty * per_unit_loss(Side.LONG, 100.0, 92.0, s)[0])
    assert z.leverage <= 10 and z.liq_price < 92.0  # likidasyon stoptan uzakta


def _worker(s):
    from backend.smc.worker import TradingWorker
    return TradingWorker(s)


def test_strategy_switch_resets_data_once_and_removes_trial_lock():
    w = _worker(bo_settings())
    w.repo.update_state(manual_hold_reasons=["expired"], equity=90.04, consecutive_losses=2)
    with w.db.session() as sess:
        from backend.smc.core.types import utc_now
        sess.add(TrialState(id=1, trial_started_at=utc_now(), trial_expires_at=utc_now()))
        sess.add(SystemEvent(level="INFO", kind="old", message="eski strateji"))
    w.holds = {"expired"}
    w._reset_for_strategy()
    st = w.repo.state()
    assert st.manual_hold_reasons == [] and st.equity == 100.0 and st.consecutive_losses == 0
    assert "expired" not in w.holds and not w.trial_expired()
    with w.db.session() as sess:
        assert sess.get(TrialState, 1) is None
        kinds = [e.kind for e in sess.query(SystemEvent).all()]
    assert "old" not in kinds and "strategy_reset" in kinds
    # ikinci başlangıçta tekrar silmez
    w.repo.system("INFO", "keep", "yeni olay")
    w._reset_for_strategy()
    with w.db.session() as sess:
        assert "keep" in [e.kind for e in sess.query(SystemEvent).all()]


def test_scan_opens_at_most_one_trade_per_4h_close(monkeypatch, filters):
    w = _worker(bo_settings())
    syms = ["AAAUSDT", "BBBUSDT"]
    w.universe = syms
    w.filters = {x: filters for x in syms}
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % H4
    for x in syms:
        cs = from_closes(channel_then(104.0, 100), tf_ms=H4, t0=t0)
        w.candles.set_history(x, "4h", cs, cs[-1].close_time + 1000)
    close_t = cs[-1].close_time
    opened: list[str] = []
    seen = []

    class P:
        def __init__(self, symbol):
            self.symbol = symbol

    async def fake_handle(ev, ctx):
        seen.append(ev)
        if ev.setup is not None:
            opened.append(ev.symbol)

    monkeypatch.setattr(w, "_handle_signal", fake_handle)
    monkeypatch.setattr(w.repo, "open_positions", lambda: [P(x) for x in opened])
    monkeypatch.setattr("backend.smc.worker.now_ms", lambda: close_t + 2000)
    run(w.scan_breakout(close_t))
    assert len(seen) == 2 and len(opened) == 1
    skipped = [e for e in seen if e.setup is None]
    assert skipped and skipped[0].reasons[-1] == "per_bar_limit:1"
