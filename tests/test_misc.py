import numpy as np

from backend.ai.simulate import simulate
from backend.risk import position_sizing as ps


def test_simulate_long_tp_and_sl():
    o = np.array([100, 100, 100, 100.0]); c = o.copy()
    h = np.array([100, 101, 103, 100.0]); l = np.array([100, 99.5, 100, 100.0])
    atr = np.full(4, 1.0)
    params = {"sl_atr_mult": 1.0, "min_stop_pct": 0.0, "tp_r": 2.0, "max_hold_bars": 3, "breakeven_r": None}
    r, bars, why = simulate(o, h, l, c, atr, np.array([0]), np.array([1]), params, 0.0)
    assert why[0] == "tp" and abs(r[0] - 2.0) < 1e-9
    l2 = np.array([100, 98.5, 100, 100.0])
    r, _, why = simulate(o, h, l2, c, atr, np.array([0]), np.array([1]), params, 0.0)
    assert why[0] == "sl" and abs(r[0] + 1.0) < 1e-9


def test_position_sizing_risk_and_liquidation_buffer():
    out = ps.calculate_position("scalp", price=100.0, dist=0.6, balance=85.0)
    assert abs(out["amount"] * 0.6 - out["risk_usd"]) < 1e-6
    assert out["risk_usd"] <= 0.85 + 1e-9
    assert out["required_margin_usd"] <= 85 * 0.30 + 1e-6
    assert out["leverage"] <= 10


def test_lab_helpers():
    from backend import lab
    n = 120
    ts = np.arange(n) * 86_400_000
    c = 100 * 1.02 ** np.arange(n)
    c[100:] = c[99] * 0.5   # trend biter -> çıkış
    a = lab.to_arrays([[int(t), x, x * 1.01, x * 0.99, x, 1] for t, x in zip(ts, c)])
    trades = lab.trend_trades("T/USDT", a, 20, 10, False, None, 86_400_000)
    assert trades and all(t["d"] == 1 for t in trades)
