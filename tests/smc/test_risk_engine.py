from datetime import datetime, timedelta, timezone

import pytest

from backend.smc.core.types import Side
from backend.smc.risk.engine import (Candidate, OpenPositionView, PortfolioState, TradingGate, daily_loss_breached,
                             evaluate_trade, register_trade_result)
from backend.smc.strategy.signal import TradeSetup
from tests.smc.conftest import make_settings

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def setup_(symbol="AAAUSDT", side=Side.LONG, entry=100.0, stop=99.0):
    s = side.sign
    r = abs(entry - stop)
    return TradeSetup(symbol, side, entry, stop, entry + s * r, entry + s * 2 * r, entry + s * 3 * r, 11, 0)


def pf(open_risks=(), equity=100.0, day_start=100.0, consec=0, cooldown=None, symbols=None, sides=None):
    syms = symbols or [f"S{i}USDT" for i in range(len(open_risks))]
    sides = sides or [Side.SHORT] * len(open_risks)
    return PortfolioState(equity, equity, day_start, [OpenPositionView(s, sd, r) for s, sd, r in zip(syms, sides, open_risks)],
                          consec, cooldown, NOW)


def cand(st=None, spread=0.0001, depth=1e6, corr=None):
    return Candidate(st or setup_(), spread, depth, corr or {})


@pytest.fixture
def cfg():
    return make_settings(taker_fee=0, slippage_pct=0, correlated_risk_factor=1.0)


def test_total_open_risk_10_plus_5_allowed(cfg, filters):
    d = evaluate_trade(cand(), pf([5, 5]), TradingGate(True), filters, cfg)
    assert d.allowed, d.reasons


def test_total_open_risk_11_plus_5_rejected(cfg, filters):
    d = evaluate_trade(cand(), pf([5, 6]), TradingGate(True), filters, cfg)
    assert not d.allowed and d.reasons[0].startswith("total_open_risk")


def test_max_three_positions(cfg, filters):
    d = evaluate_trade(cand(), pf([0, 0, 0]), TradingGate(True), filters, cfg)
    assert not d.allowed and d.reasons == ["max_open_positions"]


def test_same_symbol_rejected(cfg, filters):
    d = evaluate_trade(cand(), pf([1], symbols=["AAAUSDT"]), TradingGate(True), filters, cfg)
    assert d.reasons == ["position_exists_on_symbol"]


def test_daily_loss_limit_blocks(cfg, filters):
    p = pf(equity=89.9, day_start=100)
    assert daily_loss_breached(p, cfg)
    assert evaluate_trade(cand(), p, TradingGate(True), filters, cfg).reasons == ["daily_loss_limit"]
    assert not daily_loss_breached(pf(equity=90.5, day_start=100), cfg)


def test_gate_blocks(cfg, filters):
    d = evaluate_trade(cand(), pf(), TradingGate(False, "SAFE_MODE"), filters, cfg)
    assert d.reasons == ["gate:SAFE_MODE"]


def test_spread_and_liquidity(cfg, filters):
    assert evaluate_trade(cand(spread=0.01), pf(), TradingGate(True), filters, cfg).reasons[0].startswith("spread")
    assert evaluate_trade(cand(depth=10), pf(), TradingGate(True), filters, cfg).reasons[0].startswith("liquidity")


def test_cooldown_after_three_consecutive_sl(cfg, filters):
    c, cd = 0, None
    for _ in range(2):
        c, cd = register_trade_result(c, -5, "SL", NOW, cfg)
        assert cd is None
    c, cd = register_trade_result(c, -5, "SL", NOW, cfg)
    assert c == 0 and cd == NOW + timedelta(minutes=30)
    d = evaluate_trade(cand(), pf(cooldown=cd), TradingGate(True), filters, cfg)
    assert d.reasons == ["cooldown_active"]
    later = PortfolioState(100, 100, 100, [], 0, cd, NOW + timedelta(minutes=31))
    assert evaluate_trade(cand(), later, TradingGate(True), filters, cfg).allowed


def test_win_resets_consecutive(cfg):
    assert register_trade_result(2, 3.0, "TP3", NOW, cfg) == (0, None)
    assert register_trade_result(2, 0.0, "BREAKEVEN_STOP", NOW, cfg) == (2, None)


def test_risk_per_trade_is_5pct_of_equity(cfg, filters):
    d = evaluate_trade(cand(), pf(equity=200, day_start=200), TradingGate(True), filters, cfg)
    assert d.allowed and d.sizing.risk_target == pytest.approx(10.0)


def test_correlated_same_direction_reduces_and_blocks(filters):
    cfg = make_settings(taker_fee=0, slippage_pct=0)
    p1 = pf([5], symbols=["BTCUSDT"], sides=[Side.LONG])
    d = evaluate_trade(cand(corr={"BTCUSDT": 0.95}), p1, TradingGate(True), filters, cfg)
    assert d.allowed and d.risk_factor == 0.5 and d.sizing.risk_target == pytest.approx(2.5)
    p2 = pf([2, 2], symbols=["BTCUSDT", "ETHUSDT"], sides=[Side.LONG, Side.LONG])
    d2 = evaluate_trade(cand(corr={"BTCUSDT": 0.9, "ETHUSDT": 0.9}), p2, TradingGate(True), filters, cfg)
    assert not d2.allowed and d2.reasons == ["too_many_correlated_same_direction"]
    # ters yön korelasyon riski azaltmaz
    p3 = pf([5], symbols=["BTCUSDT"], sides=[Side.SHORT])
    assert evaluate_trade(cand(corr={"BTCUSDT": 0.95}), p3, TradingGate(True), filters, cfg).risk_factor == 1.0


def test_min_rr_enforced(filters):
    cfg = make_settings(min_rr=5.0)
    d = evaluate_trade(cand(), pf(), TradingGate(True), filters, cfg)
    assert not d.allowed and d.reasons[0].startswith("rr_below_min")
