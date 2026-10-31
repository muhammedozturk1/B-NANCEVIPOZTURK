
import pytest

from backend.smc.core.types import Side
from backend.smc.risk.plan import (TPLevel, breakeven_price, open_risk, plan_take_profits, planned_rr, r_multiple,
                           stop_after_tp, tp_prices)
from tests.smc.conftest import make_settings


def test_tp_prices_1r_2r_3r(settings):
    tps = tp_prices(Side.LONG, 100, 99, settings)
    assert [round(p, 6) for _, _, p in tps] == [101, 102, 103]
    tps_s = tp_prices(Side.SHORT, 100, 101, settings)
    assert [round(p, 6) for _, _, p in tps_s] == [99, 98, 97]


def test_tp_split_50_25_25(settings, filters):
    lv = plan_take_profits(Side.LONG, 100, 99, 4.0, filters, settings)
    assert [l.name for l in lv] == ["TP1", "TP2", "TP3"]
    assert [l.qty for l in lv] == [2.0, 1.0, 1.0]
    assert sum(l.qty for l in lv) == pytest.approx(4.0)


def test_tp_split_with_rounding_sums_to_qty(settings, filters):
    lv = plan_take_profits(Side.LONG, 100, 99, 0.007, filters, settings)
    assert sum(l.qty for l in lv) == pytest.approx(0.007)
    assert lv[0].qty == 0.003


def test_tp_merge_when_below_min_qty(settings, filters):
    from decimal import Decimal as D
    f = type(filters)(filters.symbol, filters.tick_size, D("1"), D("1"), D("1000"), D("1"), D("1000"), 5.0, 20)
    lv = plan_take_profits(Side.LONG, 100, 99, 3, f, settings)  # 1.5→1, 0.75→0 (birleşir), kalan 2
    assert sum(l.qty for l in lv) == 3
    assert all(l.qty >= 1 for l in lv)
    lv1 = plan_take_profits(Side.LONG, 100, 99, 1, f, settings)
    assert len(lv1) == 1 and lv1[0].qty == 1


def test_breakeven_long_and_short(settings):
    be_l = breakeven_price(Side.LONG, 100, settings)
    be_s = breakeven_price(Side.SHORT, 100, settings)
    buf = 100 * (2 * settings.taker_fee + settings.slippage_pct)
    assert be_l == pytest.approx(100 + buf)
    assert be_s == pytest.approx(100 - buf)
    assert be_l > 100 > be_s


def test_stop_after_tp1_moves_to_be_and_after_tp2_to_tp1(settings):
    lv = [TPLevel("TP1", 1, 101, 2), TPLevel("TP2", 2, 102, 1), TPLevel("TP3", 3, 103, 1)]
    s1 = stop_after_tp(Side.LONG, 100, lv, {"TP1"}, 99, settings)
    assert s1 == pytest.approx(breakeven_price(Side.LONG, 100, settings))
    s2 = stop_after_tp(Side.LONG, 100, lv, {"TP1", "TP2"}, s1, settings)
    assert s2 == 101
    cfg = make_settings(move_sl_to_tp1_after_tp2=False)
    assert stop_after_tp(Side.LONG, 100, lv, {"TP1", "TP2"}, s1, cfg) == s1


def test_stop_never_moves_backwards(settings):
    lv = [TPLevel("TP1", 1, 99, 2)]
    assert stop_after_tp(Side.SHORT, 100, lv, {"TP1"}, 99.5, settings) == 99.5


def test_r_multiple():
    assert r_multiple(10, 5) == 2
    assert r_multiple(-5, 5) == -1
    assert r_multiple(1, 0) == 0


def test_open_risk_zero_after_breakeven(settings):
    assert open_risk(Side.LONG, 100, 99, 2, settings) > 2
    assert open_risk(Side.LONG, 100, 100.2, 2, settings) == 0
    assert open_risk(Side.SHORT, 100, 99.5, 2, settings) == 0


def test_planned_rr_weighted(zero_fee_settings, filters):
    lv = plan_take_profits(Side.LONG, 100, 99, 4, filters, zero_fee_settings)
    assert planned_rr(Side.LONG, 100, 99, lv, zero_fee_settings) == pytest.approx(0.5 * 1 + 0.25 * 2 + 0.25 * 3)
