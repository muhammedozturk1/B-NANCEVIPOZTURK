import random

import pytest

from backend.smc.core.types import Candle
from backend.smc.strategy.liquidity import (LiquidityLevel, detect_sweep, equal_levels, previous_day_levels,
                                    swing_levels)
from backend.smc.strategy.orderflow import AggTradeDeltaAggregator, OpenInterestTracker, cvd_bias, slope, synthetic_cvd
from backend.smc.strategy.patterns import compute_ote, find_fvgs, is_displacement
from backend.smc.strategy.signal import MAX_SCORE, Component, MarketContext, evaluate_direction, score_components
from backend.smc.strategy.structure import analyze_structure, find_swings
from tests.smc.conftest import candle

T5 = 300_000


def bars(rows, tf=T5, vol=100.0):
    """rows: (high, low, close) → open = önceki close."""
    out, prev = [], rows[0][2]
    for i, (h, l, c) in enumerate(rows):
        out.append(candle(i, prev, h, l, c, v=vol, tf_ms=tf))
        prev = c
    return out


STRUCT = [(10, 9, 9.5), (11, 10, 10.5), (12, 11, 11.5), (11.5, 10.5, 11), (11, 9.8, 10), (10.5, 9, 9.5),
          (11, 9.5, 10.8), (11.8, 10, 11.7), (12.5, 11.5, 12.4), (13, 12, 12.8), (13.5, 12.5, 13.2),
          (13, 12, 12.2), (12.5, 11, 11.2), (12, 8.5, 8.8)]


# ------------------------------------------------------------- structure
def test_swing_detection_pivot_2_2():
    sw = find_swings(bars(STRUCT), 2, 2)
    highs = [(s.index, s.price) for s in sw if s.kind == "high"]
    lows = [(s.index, s.price) for s in sw if s.kind == "low"]
    assert (2, 12) in highs and (10, 13.5) in highs
    assert (5, 9) in lows
    assert all(s.confirm_index == s.index + 2 for s in sw)


def test_swing_requires_strictly_higher():
    rows = [(10, 9, 9.5), (11, 9, 10), (12, 9, 11), (12, 9, 11), (11, 9, 10), (10, 9, 9.5)]
    assert not [s for s in find_swings(bars(rows)) if s.kind == "high"]


def test_bos_on_close_and_choch():
    st = analyze_structure(bars(STRUCT))
    kinds = [(e.index, e.kind, e.direction, e.level) for e in st.events]
    assert (8, "BOS", "bullish", 12) in kinds
    assert (13, "CHOCH", "bearish", 9) in kinds
    assert st.trend == "bearish"


def test_wick_only_is_not_bos():
    rows = STRUCT[:8] + [(12.6, 11.5, 11.9)]  # high 12'yi geçer ama close 11.9
    st = analyze_structure(bars(rows))
    assert not [e for e in st.events if e.direction == "bullish"]


# ------------------------------------------------------------- liquidity
SWEEP = [(12, 11, 11.5), (11.8, 10.8, 11), (11.5, 10, 10.2), (10.8, 9.5, 10.5), (10.6, 9.8, 10), (10.2, 9, 9.4),
         (10.5, 9.3, 10.3), (11, 10, 10.8), (10.7, 9.6, 9.8), (10, 8.7, 9.6), (10.6, 9.5, 10.5), (11.3, 10.4, 11.2)]


def test_liquidity_sweep_with_structure_confirmation():
    c = bars(SWEEP)
    st = analyze_structure(c)
    levels = swing_levels(st.swings, c, external=False)
    sw = detect_sweep(c, levels, st, "bullish", lookback=36, reclaim_bars=2)
    assert sw is not None
    assert sw.index == 9 and sw.level.price == 9 and sw.extreme == 8.7
    assert sw.confirm.index == 11 and sw.confirm.direction == "bullish"


def test_touch_only_is_not_sweep():
    rows = list(SWEEP)
    rows[9] = (10, 9.0, 9.6)  # seviyeye sadece dokunuyor (low == 9)
    c = bars(rows)
    st = analyze_structure(c)
    sw = detect_sweep(c, [l for l in swing_levels(st.swings, c, False) if l.price == 9], st, "bullish")
    assert sw is None


def test_failed_reclaim_is_not_sweep():
    rows = SWEEP[:9] + [(10, 8.7, 8.8), (9, 8.5, 8.6), (8.9, 8.2, 8.3), (9.2, 8.4, 8.9)]
    c = bars(rows)
    st = analyze_structure(c)
    lv = [LiquidityLevel(9, "sell", "swing_internal", c[7].close_time, 1)]
    assert detect_sweep(c, lv, st, "bullish", reclaim_bars=2) is None


def test_sweep_requires_confirmation():
    c = bars(SWEEP[:11])  # BOS mumu yok
    st = analyze_structure(c)
    assert detect_sweep(c, swing_levels(st.swings, c, False), st, "bullish") is None


def test_equal_levels_and_previous_day():
    c = bars(STRUCT)
    sw = find_swings(c)
    eq = equal_levels(sw, c, tolerance=0.5)
    assert eq and all(l.kind == "equal" for l in eq)
    day_ms = 86_400_000
    cs = [candle(i, 100, 101 + (i < 4), 99 - (i < 4), 100, tf_ms=day_ms // 4, t0=1_700_006_400_000 - 1_700_006_400_000 % day_ms)
          for i in range(8)]
    pd = previous_day_levels(cs)
    assert {l.kind for l in pd} == {"pdh", "pdl"}
    assert [l.price for l in pd] == [102, 98]


# ------------------------------------------------------------ OTE / FVG
def test_ote_long_and_short():
    z = compute_ote("bullish", 100, 110)
    assert z.low == pytest.approx(102.1) and z.high == pytest.approx(103.8) and z.preferred == pytest.approx(102.95)
    assert z.contains(103) and not z.contains(105)
    zs = compute_ote("bearish", 110, 100)
    assert zs.low == pytest.approx(106.2) and zs.high == pytest.approx(107.9) and zs.preferred == pytest.approx(107.05)
    with pytest.raises(ValueError):
        compute_ote("bullish", 110, 100)


def test_fvg_bullish_and_bearish():
    up = bars([(10, 9, 9.8), (11.5, 9.9, 11.4), (12, 10.5, 11.8)])
    f = find_fvgs(up, 0, 2, "bullish")
    assert len(f) == 1 and f[0].low == 10 and f[0].high == 10.5
    dn = bars([(12, 11, 11.2), (11.1, 9.5, 9.6), (10.5, 9, 9.2)])
    fb = find_fvgs(dn, 0, 2, "bearish")
    assert len(fb) == 1 and fb[0].low == 10.5 and fb[0].high == 11
    assert not find_fvgs(up, 0, 2, "bearish")


def test_displacement():
    cs = [candle(i, 100, 100.15, 99.95, 100.1, v=100) for i in range(20)]
    cs.append(candle(20, 100.1, 100.65, 100.05, 100.6, v=200))  # gövde 0.5 ≥ 1.5×0.1, high'a yakın kapanış
    assert is_displacement(cs, 20, "bullish")
    assert not is_displacement(cs, 20, "bearish")
    weak = cs[:20] + [candle(20, 100.1, 100.9, 100.05, 100.6, v=200)]  # kapanış lokasyonu zayıf
    assert not is_displacement(weak, 20, "bullish")


# ------------------------------------------------------------- orderflow
def test_synthetic_delta_and_cvd():
    cs = [Candle(i, 1, 1, 1, 1, 10, i + 1, taker_buy_volume=b) for i, b in enumerate([6, 7, 3, 5])]
    assert [c.delta for c in cs] == [2, 4, -4, 0]
    assert synthetic_cvd(cs, 50) == [2, 6, 2, 2]
    assert synthetic_cvd(cs, 2) == [-4, -4]
    assert slope([0, 1, 2, 3]) == pytest.approx(1.0)
    rising = [Candle(i, 1, 1, 1, 1, 10, i + 1, taker_buy_volume=7) for i in range(30)]
    assert cvd_bias(rising) > 0


def test_aggtrade_delta_aggregator():
    agg = AggTradeDeltaAggregator(bucket_ms=60_000)
    agg.add(1_000, 2.0, buyer_is_maker=False)  # agresif alış
    agg.add(2_000, 0.5, buyer_is_maker=True)  # agresif satış
    agg.add(61_000, 1.0, buyer_is_maker=True)
    assert agg.delta(0) == pytest.approx(1.5) and agg.delta(60_000) == pytest.approx(-1.0)


def test_oi_tracker():
    oi = OpenInterestTracker()
    oi.add("X", 0, 1000, 100)
    oi.add("X", 600_000, 1010, 101)
    chg = oi.change("X")
    assert chg == pytest.approx((0.01, 0.01))


# --------------------------------------------------------------- scoring
def test_score_table_sums_to_15():
    table = {"htf_structure": 2, "ltf_structure": 2, "liquidity_sweep": 2, "ote": 1, "displacement": 1, "fvg": 1,
             "cvd": 2, "volume": 1, "oi": 1, "funding": 1, "macro": 1}
    comps = [Component(k, v, v, True) for k, v in table.items()]
    assert score_components(comps) == MAX_SCORE == 15
    comps[0].points = 5  # puan max'ı aşamaz
    assert score_components(comps) == 15


def test_insufficient_data_rejected(settings):
    ev = evaluate_direction("X", "bullish", [], [], [], MarketContext(), settings)
    assert ev.setup is None and "insufficient_data" in ev.reasons


def _random_series(n, tf, seed, start=100.0):
    rng = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        p = max(1.0, p * (1 + rng.gauss(0, 0.004)))
        h, l = max(o, p) * (1 + abs(rng.gauss(0, 0.001))), min(o, p) * (1 - abs(rng.gauss(0, 0.001)))
        v = rng.uniform(50, 150)
        out.append(Candle(i * tf, o, h, l, p, v, (i + 1) * tf - 1, v * rng.uniform(0.3, 0.7)))
    return out


def test_full_pipeline_components_and_mandatory_rule(settings):
    c15 = _random_series(200, 900_000, 1)
    c5 = _random_series(400, 300_000, 2)
    c3 = _random_series(300, 180_000, 3)
    seen_full = False
    for end in range(150, 400, 3):
        for d in ("bullish", "bearish"):
            ev = evaluate_direction("X", d, c15, c5[:end], c3, MarketContext(funding_rate=0.0001, fear_greed=50), settings)
            assert ev.score <= 15
            if len(ev.components) == 12:
                seen_full = True
                assert sum(c.max_points for c in ev.components) == 15
            failed = [c for c in ev.components if c.mandatory and not c.passed]
            if failed:
                assert ev.setup is None
            if ev.setup is not None:
                assert ev.score >= settings.min_score
    assert seen_full
