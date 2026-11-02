import pytest

from backend.smcbot import config
from backend.smcbot.orderflow import FlowSnapshot
from backend.smcbot.strategy import SymbolAnalyzer
from tests.smcbot.helpers import long_case, mirror_case

S = dict(config.DEFAULT_SETTINGS)


def test_long_setup_detected():
    ltf, htf = long_case()
    an = SymbolAnalyzer("TESTUSDT", ltf, htf)
    t = len(ltf) - 1
    sig, near = an.evaluate(t, S, min_score=0)
    assert sig is not None, f"kurulum bulunamadı, aday: {near}"
    assert sig.side == "LONG"
    # FVG: bar169 tepesi 101.0 ile bar171 dibi 102.4 arası, CE = 101.7
    assert sig.entry == pytest.approx(101.7, abs=1e-6)
    assert sig.stop < 99.6
    assert sig.tp2 > sig.tp1 > sig.entry
    assert sig.rr >= S["min_rr"]
    texts = " | ".join(r["text"] for r in sig.reasons)
    assert "eşit dipler" in texts
    assert "FVG" in texts
    assert "MSS" in texts
    assert sig.annotations["zone"]["type"] == "FVG"
    assert sig.annotations["sweep"]["price"] == pytest.approx(99.6)


def test_no_signal_one_bar_earlier():
    ltf, htf = long_case()
    an = SymbolAnalyzer("TESTUSDT", ltf, htf)
    sig, _ = an.evaluate(len(ltf) - 2, S, min_score=0)
    assert sig is None  # MSS henüz olmadı -> erken sinyal yok (ileriye bakma yok)


def test_short_is_exact_mirror():
    ltf, htf = long_case()
    mltf, mhtf = mirror_case(ltf, htf)
    a = SymbolAnalyzer("X", ltf, htf).evaluate(len(ltf) - 1, S, min_score=0)[0]
    b = SymbolAnalyzer("X", mltf, mhtf).evaluate(len(ltf) - 1, S, min_score=0)[0]
    assert b is not None and b.side == "SHORT"
    assert b.entry == pytest.approx(200 - a.entry)
    assert b.stop == pytest.approx(200 - a.stop)
    # yüzdesel parametreler (98 vs 102 fiyat ölçeği) hedefte küçük fark yaratabilir
    assert b.tp2 == pytest.approx(200 - a.tp2, abs=0.3)
    assert abs(b.structure_score - a.structure_score) <= 5  # hedef türü ölçeğe göre değişebilir


def test_negation_mirror_is_exact():
    from backend.smcbot.strategy import SymbolAnalyzer as SA
    ltf, htf = long_case()
    an = SA("X", ltf, htf)
    t = len(ltf) - 1
    (A, _), (Am, _) = an.views["LONG"], an.views["SHORT"]
    from backend.smcbot.smc import find_long
    r1 = find_long(A, t, 0.3, 4)
    # aynalanmış serinin aynası orijinali verir
    from backend.smcbot.smc import Analysis
    r2 = find_long(Analysis(Am.c.mirror()), t, 0.3, 4)
    assert r1 == r2


def test_flow_scoring_and_mirror():
    ltf, htf = long_case()
    an = SymbolAnalyzer("X", ltf, htf)
    t = len(ltf) - 1
    base, _ = an.evaluate(t, S, min_score=0)
    good = FlowSnapshot(liq_against_usd=400_000, liq_baseline_usd=50_000, oi_change_pct=-1.2,
                        funding=-0.0001, ob_imbalance=0.3)
    sig, _ = an.evaluate(t, S, flow=good, min_score=0)
    assert sig.flow_score == 25
    bad = FlowSnapshot(funding=0.0008, ob_imbalance=-0.4)
    sig2, _ = an.evaluate(t, S, flow=bad, min_score=0)
    assert sig2.flow_score == -15
    assert sig2.score < base.score

    m = good.mirrored()
    assert m.funding == pytest.approx(0.0001) and m.ob_imbalance == pytest.approx(-0.3)


def test_threshold_returns_near_miss():
    ltf, htf = long_case()
    an = SymbolAnalyzer("X", ltf, htf)
    sig, near = an.evaluate(len(ltf) - 1, S, min_score=101)
    assert sig is None and near is not None
