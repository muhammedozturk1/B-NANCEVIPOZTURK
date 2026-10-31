from datetime import timedelta

import pytest

from backend.smc.core.trial import ensure_trial, trial_status
from backend.smc.core.types import Side, ensure_utc, utc_now
from backend.smc.execution.kill_switch import flatten_all
from backend.smc.execution.reconciliation import reconcile
from backend.smc.execution.state_machine import InvalidTransition, PosState, assert_transition, can_transition
from backend.smc.execution.trade_manager import TradeManager
from backend.smc.risk.engine import Candidate, PortfolioState, TradingGate, evaluate_trade
from backend.smc.strategy.signal import TradeSetup
from tests.smc.conftest import make_settings, run
from tests.smc.fakes import FakeExchange

S = PosState


def build(repo, notifier, filters, ex=None, **cfg):
    cfg.setdefault("entry_order_timeout_s", 0.05)
    s = make_settings(**cfg)
    ex = ex or FakeExchange()
    safe = []

    async def on_safe(reason, manual):
        safe.append((reason, manual))

    tm = TradeManager(ex, repo, notifier, s, lambda sym: filters, on_safe, poll_interval=0.01)
    return tm, ex, s, safe


def decide(s, filters, side=Side.LONG):
    st = TradeSetup("TESTUSDT", side, 100.0, 99.0 if side is Side.LONG else 101.0,
                    101 if side is Side.LONG else 99, 102 if side is Side.LONG else 98,
                    103 if side is Side.LONG else 97, 12, 0)
    pf = PortfolioState(100, 100, 100, [], 0, None, utc_now())
    d = evaluate_trade(Candidate(st, 0.0001, 1e6, {}), pf, TradingGate(True), filters, s)
    assert d.allowed, d.reasons
    return st, d


# ---------------------------------------------------------- state machine
def test_state_machine_transitions():
    assert can_transition(S.SIGNAL_DETECTED, S.RISK_CHECK)
    assert can_transition(S.ACTIVE, S.CLOSED)  # SL her an
    assert can_transition(S.TP1_FILLED, S.BREAKEVEN_ACTIVE)
    assert not can_transition(S.CLOSED, S.ACTIVE)
    assert not can_transition(S.SIGNAL_DETECTED, S.ACTIVE)
    with pytest.raises(InvalidTransition):
        assert_transition(S.ORDER_REJECTED, S.ACTIVE)


# ----------------------------------------------------------------- entry
def test_full_open_places_native_sl_and_tps(repo, notifier, filters):
    tm, ex, s, safe = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    assert pos is not None and pos.state == S.ACTIVE.value
    orders = repo.orders_for_position(pos.id)
    purposes = [o.purpose for o in orders]
    assert purposes.count("SL") == 1 and {"TP1", "TP2", "TP3"} <= set(purposes)
    sl = next(o for o in orders if o.purpose == "SL")
    assert sl.is_algo and sl.type == "STOP_MARKET" and sl.qty == pos.filled_qty
    assert ex.setup_calls == [("TESTUSDT", d.sizing.leverage)]
    assert "NEW_LONG" in notifier.kinds()
    assert sum(l["qty"] for l in pos.tp_plan) == pytest.approx(pos.filled_qty)


def test_partial_fill_uses_filled_qty_only(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters, ex=FakeExchange(fill_ratio=0.4))
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    assert pos.filled_qty == pytest.approx(round(d.sizing.qty * 0.4, 3))
    assert pos.filled_qty < d.sizing.qty
    sl = repo.active_order(pos.id, "SL")
    assert sl.qty == pytest.approx(pos.filled_qty)
    tps = [o for o in repo.orders_for_position(pos.id) if o.purpose.startswith("TP")]
    assert sum(o.qty for o in tps) == pytest.approx(pos.filled_qty)
    entry = repo.active_order(pos.id, "ENTRY") or [o for o in repo.orders_for_position(pos.id) if o.purpose == "ENTRY"][0]
    assert ex.orders[entry.client_id].status == "CANCELED"  # kalan miktar timeout sonrası iptal


def test_unfilled_entry_closes_position(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters, ex=FakeExchange(fill_ratio=0.0))
    st, d = decide(s, filters)
    assert run(tm.open_position(st, d, None)) is None
    p = repo.closed_positions()[0]
    assert p.exit_reason == "ENTRY_NOT_FILLED"


def test_sl_failure_triggers_emergency_close_and_safe_mode(repo, notifier, filters):
    tm, ex, s, safe = build(repo, notifier, filters, ex=FakeExchange(fail_stop=True))
    st, d = decide(s, filters)
    assert run(tm.open_position(st, d, None)) is None
    assert not ex.positions_  # pozisyon kapatıldı
    p = repo.closed_positions()[0]
    assert p.exit_reason == "SL_FAILURE"
    assert safe == [("sl_placement_failed", True)]
    assert "SL_FAILURE" in notifier.kinds()


# ------------------------------------------------------- manage / TP / BE
def test_tp1_moves_sl_to_breakeven_then_tp2_to_tp1_then_tp3_closes(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    tp1 = next(o for o in repo.orders_for_position(pos.id) if o.purpose == "TP1")
    ex.trigger(tp1.client_id)
    run(tm.manage_all())
    pos = repo.get_position(pos.id)
    assert pos.state == S.BREAKEVEN_ACTIVE.value and pos.be_active
    assert pos.stop_price > pos.entry_price  # long BE + fee buffer
    sl = repo.active_order(pos.id, "SL")
    assert sl.qty == pytest.approx(pos.remaining_qty)
    assert pos.open_risk == 0
    assert "BREAKEVEN" in notifier.kinds()

    tp2 = next(o for o in repo.orders_for_position(pos.id) if o.purpose == "TP2")
    ex.trigger(tp2.client_id)
    run(tm.manage_all())
    pos = repo.get_position(pos.id)
    assert pos.state == S.TP2_FILLED.value and pos.stop_price == pytest.approx(pos.tp1_price)

    tp3 = next(o for o in repo.orders_for_position(pos.id) if o.purpose == "TP3")
    ex.trigger(tp3.client_id)
    run(tm.manage_all())
    pos = repo.get_position(pos.id)
    assert pos.state == S.CLOSED.value and pos.exit_reason == "TP3"
    t = repo.trades()[0]
    assert t.net_pnl > 0 and t.realized_rr > 0 and set(t.tp_hits) == {"TP1", "TP2", "TP3"}
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.commission + t.funding)


def test_sl_hit_records_loss_and_consecutive_counter(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    sl = repo.active_order(pos.id, "SL")
    ex.trigger(sl.client_id)
    run(tm.manage_all())
    pos = repo.get_position(pos.id)
    assert pos.state == S.CLOSED.value and pos.exit_reason == "SL"
    assert repo.trades()[0].net_pnl < 0
    assert repo.state().consecutive_losses == 1
    assert not [o for o in ex.orders.values() if o.active]  # kalan TP'ler iptal


def test_missing_sl_is_replaced(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    sl = repo.active_order(pos.id, "SL")
    ex.orders[sl.client_id].status = "CANCELED"  # birisi SL'yi elle iptal etti
    run(tm.manage_all())
    new_sl = repo.active_order(pos.id, "SL")
    assert new_sl.client_id != sl.client_id and ex.orders[new_sl.client_id].active


# -------------------------------------------------------- reconciliation
def test_reconcile_finalizes_position_closed_while_offline(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    ex.trigger(repo.active_order(pos.id, "SL").client_id)  # bot kapalıyken SL tetiklendi
    rep = run(reconcile(ex, repo, tm))
    assert rep.clean and repo.get_position(pos.id).state == S.CLOSED.value


def test_reconcile_orphan_position_requires_manual(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    ex.open_position("ORPHANUSDT", 1.0, 50)
    rep = run(reconcile(ex, repo, tm))
    assert not rep.clean and "orphan" in rep.issues[0]


def test_reconcile_replaces_missing_sl_and_quantity_mismatch(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    for o in ex.orders.values():
        o.status = "CANCELED"
    rep = run(reconcile(ex, repo, tm))
    assert rep.clean and any("SL" in a for a in rep.actions)
    ex.positions_["TESTUSDT"].qty += 5  # borsa miktarı DB'den büyük
    rep2 = run(reconcile(ex, repo, tm))
    assert not rep2.clean and repo.get_position(pos.id).state == S.RECONCILIATION_REQUIRED.value


def test_reconcile_never_submitted_positions_closed(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    p = repo.create_position(symbol="TESTUSDT", side="LONG", state=S.RISK_CHECK.value, leverage=10, planned_entry=100,
                             requested_qty=1, initial_stop=99, stop_price=99, risk_amount=1)
    rep = run(reconcile(ex, repo, tm))
    assert rep.clean and repo.get_position(p.id).state == S.ORDER_REJECTED.value


# ----------------------------------------------------------- kill switch
def test_kill_switch_flattens_everything(repo, notifier, filters):
    tm, ex, s, _ = build(repo, notifier, filters)
    st, d = decide(s, filters)
    pos = run(tm.open_position(st, d, None))
    ex.open_position("OTHERUSDT", -2.0, 10)
    res = run(flatten_all(ex, repo, tm, "KILL_SWITCH", delay=0))
    assert res.ok
    assert not ex.positions_ and not [o for o in ex.orders.values() if o.active]
    p = repo.get_position(pos.id)
    assert p.state == S.CLOSED.value and p.exit_reason == "KILL_SWITCH"


# ----------------------------------------------------------------- trial
def test_trial_persists_and_expires(db):
    now = utc_now()
    with db.session() as s:
        row = ensure_trial(s, now, 72)
        exp = row.trial_expires_at
    with db.session() as s:  # "restart": süre yeniden başlamaz
        row2 = ensure_trial(s, now + timedelta(hours=10), 72)
        assert ensure_utc(row2.trial_expires_at) == ensure_utc(exp)
        st = trial_status(row2, now + timedelta(hours=71))
        assert not st.expired and st.remaining_s == pytest.approx(3600, abs=1)
        assert trial_status(row2, now + timedelta(hours=72, seconds=1)).expired
