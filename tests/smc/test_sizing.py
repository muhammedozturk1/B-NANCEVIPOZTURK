from decimal import Decimal

import pytest

from backend.smc.core.types import Side
from backend.smc.risk.sizing import (compute_position_size, estimate_liquidation_price, fmt_decimal, per_unit_loss,
                             round_price, round_stop_price, round_to_step, round_tp_price)
from tests.smc.conftest import make_settings


def test_spec_example_risk_5_usdt_notional_500_margin_50(zero_fee_settings, filters):
    r = compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=99,
                              filters=filters, cfg=zero_fee_settings)
    assert r.ok
    assert r.risk_target == pytest.approx(5.0)
    assert r.qty == pytest.approx(5.0)
    assert r.notional == pytest.approx(500.0)
    assert r.leverage == 10
    assert r.margin == pytest.approx(50.0)
    # Risk marjin değildir: SL'de kaybedilen ≈ 5 USDT, marjin 50 USDT
    assert r.risk_actual == pytest.approx(5.0)


def test_risk_is_dynamic_with_equity(zero_fee_settings, filters):
    r = compute_position_size(side=Side.LONG, equity=120, available_balance=120, entry=100, stop=99,
                              filters=filters, cfg=zero_fee_settings)
    assert r.risk_target == pytest.approx(6.0)
    assert r.qty == pytest.approx(6.0)


def test_fees_and_slippage_reduce_size_so_total_loss_within_budget(settings, filters):
    r = compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=99,
                              filters=filters, cfg=settings)
    pul, fees, slip = per_unit_loss(Side.LONG, 100, 99, settings)
    assert pul == pytest.approx(1 + 199 * 0.0005 + 199 * 0.0005)
    assert r.qty < 5.0
    assert r.risk_actual <= 5.0 + 1e-9
    assert r.qty * pul == pytest.approx(r.risk_actual)


def test_short_side(zero_fee_settings, filters):
    r = compute_position_size(side=Side.SHORT, equity=100, available_balance=100, entry=100, stop=101,
                              filters=filters, cfg=zero_fee_settings)
    assert r.ok and r.qty == pytest.approx(5.0)


def test_stop_on_wrong_side_rejected(settings, filters):
    assert not compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=101,
                                     filters=filters, cfg=settings).ok


def test_leverage_respects_exchange_maximum(zero_fee_settings, filters):
    f5 = filters.with_bracket(5, 0.004, 0.0)
    r = compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=99,
                              filters=f5, cfg=zero_fee_settings)
    # 500 notional / 5x = 100 marjin > 95 kullanılabilir → miktar marjine göre kısılır
    assert r.ok and r.leverage <= 5
    assert r.margin <= 100 * zero_fee_settings.max_margin_usage + 1e-9
    assert r.capped_by == "available_margin"


def test_leverage_never_above_10(filters):
    cfg = make_settings(taker_fee=0, slippage_pct=0)
    f50 = filters.with_bracket(50, 0.004, 0.0)
    r = compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=99,
                              filters=f50, cfg=cfg)
    assert r.leverage <= 10


def test_leverage_does_not_increase_position_size(filters):
    """Kaldıraç artırılsa da risk tabanlı miktar değişmez."""
    cfg3 = make_settings(taker_fee=0, slippage_pct=0, max_leverage=3, max_position_notional=1e6)
    cfg10 = make_settings(taker_fee=0, slippage_pct=0, max_leverage=10, max_position_notional=1e6)
    r3 = compute_position_size(side=Side.LONG, equity=1000, available_balance=10000, entry=100, stop=99,
                               filters=filters, cfg=cfg3)
    r10 = compute_position_size(side=Side.LONG, equity=1000, available_balance=10000, entry=100, stop=99,
                                filters=filters, cfg=cfg10)
    assert r3.qty == r10.qty == pytest.approx(50.0)
    assert r3.margin > r10.margin


def test_max_position_notional_cap(filters):
    cfg = make_settings(taker_fee=0, slippage_pct=0, max_position_notional=200)
    r = compute_position_size(side=Side.LONG, equity=100, available_balance=100, entry=100, stop=99.5,
                              filters=filters, cfg=cfg)
    assert r.notional <= 200 + 1e-9 and r.capped_by == "max_notional"


def test_liquidation_buffer_lowers_leverage_or_rejects(filters):
    cfg = make_settings(taker_fee=0, slippage_pct=0, max_stop_pct=0.2)
    # %8 stop: 10x'te liq ~%9.6 uzakta → buffer yetersiz, kaldıraç düşürülür
    r = compute_position_size(side=Side.LONG, equity=1000, available_balance=1000, entry=100, stop=92,
                              filters=filters, cfg=cfg)
    assert r.ok and r.leverage < 10
    assert r.liq_price < 92 - max(0.5, 0.5 * 8) + 1e-9
    # marjin yetmiyorsa reddedilir
    r2 = compute_position_size(side=Side.LONG, equity=100, available_balance=5, entry=100, stop=92,
                               filters=filters, cfg=cfg)
    assert not r2.ok


def test_liquidation_formula():
    liq_l = estimate_liquidation_price(Side.LONG, 100, 1, 10, 0.004)
    liq_s = estimate_liquidation_price(Side.SHORT, 100, 1, 10, 0.004)
    assert liq_l == pytest.approx(90 / 0.996)
    assert liq_s == pytest.approx(110 / 1.004)


def test_qty_below_min_rejected(zero_fee_settings, filters):
    from decimal import Decimal as D
    big_min = type(filters)(filters.symbol, filters.tick_size, D("1"), D("1"), D("1000"), D("1"), D("1000"), 5.0, 20)
    r = compute_position_size(side=Side.LONG, equity=1, available_balance=1, entry=100, stop=90,
                              filters=big_min, cfg=zero_fee_settings)
    assert not r.ok and r.reason == "qty_below_min"


# ---------------------------------------------------------------- rounding
def test_quantity_rounding_always_down():
    assert round_to_step(1.23456, Decimal("0.001")) == 1.234
    assert round_to_step(1.9999, Decimal("1")) == 1.0
    assert round_to_step(0.0009, Decimal("0.001")) == 0.0
    assert round_to_step(12.345, Decimal("0.01"), "up") == 12.35


def test_tick_rounding():
    assert round_price(100.126, Decimal("0.01")) == pytest.approx(100.13)
    assert round_price(100.126, Decimal("0.01"), "down") == pytest.approx(100.12)
    assert round_price(100.121, Decimal("0.05"), "up") == pytest.approx(100.15)


def test_stop_rounded_away_from_entry_tp_toward_entry():
    t = Decimal("0.1")
    assert round_stop_price(98.97, Side.LONG, t) == pytest.approx(98.9)
    assert round_stop_price(101.03, Side.SHORT, t) == pytest.approx(101.1)
    assert round_tp_price(102.07, Side.LONG, t) == pytest.approx(102.0)
    assert round_tp_price(97.93, Side.SHORT, t) == pytest.approx(98.0)


def test_fmt_decimal_no_scientific():
    assert fmt_decimal(0.00001, Decimal("0.00001")) == "0.00001"
    assert fmt_decimal(12.0, Decimal("1")) == "12"
