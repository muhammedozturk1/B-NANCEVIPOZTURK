"""SMC Scalper — scalp şartnamesi: sinyal kuralları (LONG/SHORT ve her filtre), sabit 2 USDT risk boyutlama,
tek TP (1,4R), ayar türetimi (1 pozisyon, 10x, 200 USDT, Telegram kapalı) ve pozisyon yönetimi
(0,7R'de başabaş, 6 dk süre, ters güçlü mum)."""
from datetime import timedelta
from decimal import Decimal

import pytest

from backend.smc.core.types import Candle, Side, ensure_utc, utc_now
from backend.smc.execution.state_machine import PosState
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.risk.engine import Candidate, PortfolioState, TradingGate, evaluate_trade
from backend.smc.risk.sizing import compute_position_size
from backend.smc.strategy import scalp as SC
from backend.smc.strategy.signal import TradeSetup
from tests.smc.conftest import make_settings, run
from tests.smc.test_execution import build

T0 = 1_700_006_400_000          # 00:00 UTC'ye denk gelen bir an (işlem saatleri filtresi için +6 saat eklenir)
M1, M5 = 60_000, 300_000
DAY_OK = T0 + 8 * 3_600_000     # 08:00 UTC


def c(t, o, h, l, cl, v=100.0, tf=M1):
    return Candle(t, o, h, l, cl, v, t + tf - 1)


def trend5(up=True, n=100, start=DAY_OK - 100 * M5):
    out, p = [], 100.0
    for i in range(n):
        q = p * (1.0006 if up else 0.9994)
        out.append(c(start + i * M5, p, max(p, q) * 1.0002, min(p, q) * 0.9998, q, 1000, M5))
        p = q
    return out


def setup_1m(up=True, vol=300.0, body_frac=0.8, last_t=DAY_OK, base=None, prev_wick=False):
    """59 sakin mum + güçlü kırılım mumu. base: 5 dk trendinin son kapanışına yakın fiyat."""
    sg = 1 if up else -1
    p = base or 100.0
    out = []
    for i in range(59):
        t = last_t - (59 - i) * M1
        # küçük zigzag: RSI orta bölgede kalsın
        d = 0.0003 * p * (1 if i % 2 else -1)
        o, cl = p, p + d
        out.append(c(t, o, max(o, cl) + 0.0001 * p, min(o, cl) - 0.0001 * p, cl, 100.0))
    if prev_wick:  # kırılımı bozmayacak yönde (LONG: alt, SHORT: üst) çok uzun fitil
        t = last_t - M1
        o = out[-1].close
        out[-1] = c(t, o, o + (0.0001 if up else 0.004) * p, o - (0.004 if up else 0.0001) * p, o, 100.0)
    o = out[-1].close
    rng = 0.0025 * p
    cl = o + sg * rng * body_frac
    hi, lo = (cl + rng * (1 - body_frac) / 2, o - rng * (1 - body_frac) / 2) if up else \
        (o + rng * (1 - body_frac) / 2, cl - rng * (1 - body_frac) / 2)
    out.append(c(last_t, o, hi, lo, cl, vol))
    return out


def P(**kw):
    return SC.ScalpParams(**kw)


def eval_(up=True, **kw):
    c5 = trend5(up)
    base = c5[-1].close
    c1 = setup_1m(up, base=base, **kw)
    return SC.evaluate_scalp("BTCUSDT", c5, c1, P())


# ------------------------------------------------------------------ sinyal
@pytest.mark.parametrize("up", [True, False])
def test_valid_breakout_long_and_short(up):
    ev = eval_(up)
    assert ev is not None and ev.reasons == [], ev.reasons
    s = ev.setup
    assert s.side is (Side.LONG if up else Side.SHORT)
    b = ev.snapshot["planned"]
    assert (s.stop < s.entry) == up and b["stop_pct"] <= 0.0038
    assert abs(s.tp1 - s.entry) == pytest.approx(1.4 * abs(s.entry - s.stop))


@pytest.mark.parametrize("up", [True, False])
def test_each_filter_blocks(up):
    assert "volume" in eval_(up, vol=150).reasons                     # 1,5× < 1,8×
    assert "body" in eval_(up, body_frac=0.4).reasons                 # gövde %40
    assert "wick" in eval_(up, prev_wick=True).reasons                # önceki mumda uzun fitil
    assert "session" in eval_(up, last_t=T0 + 2 * 3_600_000).reasons  # 02:00 UTC
    # 5 dk ters yön
    c5 = trend5(not up)
    c1 = setup_1m(up, base=c5[-1].close)
    assert "trend:against" in SC.evaluate_scalp("X", c5, c1, P()).reasons
    # EMA'ya yapışık: eşiği çok büyük tut
    c5 = trend5(up)
    ev = SC.evaluate_scalp("X", c5, setup_1m(up, base=c5[-1].close), P(ema_min_dist_pct=0.5))
    assert "trend:sticky" in ev.reasons
    # stop mesafesi > %0,38
    ev = SC.evaluate_scalp("X", c5, setup_1m(up, base=c5[-1].close), P(max_stop_pct=0.001))
    assert "stop_distance" in ev.reasons and ev.setup is None


def test_rsi_extreme_blocks_long():
    c5 = trend5(True)
    p = c5[-1].close
    c1 = []
    for i in range(59):  # sürekli yükseliş: RSI(5) ~100
        o = p * (1 + 0.0004 * i)
        c1.append(c(DAY_OK - (59 - i) * M1, o, o * 1.0005, o * 0.9999, o * 1.0004, 100.0))
    o = c1[-1].close
    c1.append(c(DAY_OK, o, o * 1.0026, o * 0.9999, o * 1.0024, 400.0))
    ev = SC.evaluate_scalp("X", c5, c1, P())
    assert ev is not None and "rsi" in ev.reasons


def test_no_breakout_returns_none():
    c5 = trend5(True)
    c1 = setup_1m(True, base=c5[-1].close)[:-1]
    c1.append(c(DAY_OK, c1[-1].close, c1[-1].close, c1[-1].close * 0.9999, c1[-1].close, 300))
    assert SC.evaluate_scalp("X", c5, c1, P()) is None


def test_indicators():
    assert SC.ema([1.0] * 30, 20) == pytest.approx(1.0)
    assert SC.rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5) == 100.0
    assert SC.rsi([10, 9, 8, 7, 6, 5, 4, 3], 5) == 0.0
    assert SC.in_no_trade_window(T0 + 90 * M1, P()) and not SC.in_no_trade_window(T0 + 330 * M1, P())


# ------------------------------------------------------------------ ayar ve boyutlama
def scalp_settings(**kw):
    base = dict(strategy="scalp", taker_fee=0.0005, slippage_pct=0.0002)
    base.update(kw)
    return make_settings(**base)


def test_settings_derived_for_scalp():
    s = scalp_settings()
    assert s.max_open_positions == 1 and s.max_leverage == 10 and s.starting_equity == 200
    assert (s.tp1_pct, s.tp2_pct, s.tp3_pct) == (1.0, 0.0, 0.0) and s.tp1_r == 1.4
    assert s.daily_loss_limit == 1.0 and s.max_consecutive_sl > 10 ** 6 and s.min_rr < 0
    assert s.whitelist == ["BTCUSDT", "ETHUSDT"]
    assert s.telegram_enabled is False


F = SymbolFilters("BTCUSDT", Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("1000"),
                  Decimal("0.001"), Decimal("1000"), 100.0, 125, 0.004, 0.0)


def test_sizing_fixed_two_usdt_and_rejections():
    s = scalp_settings()
    entry, stop = 90_000.0, 89_820.0          # %0,2 stop -> miktar 2/180 = 0,0111 -> 0,011 BTC
    r = compute_position_size(side=Side.LONG, equity=200, available_balance=5000, entry=entry, stop=stop,
                              filters=F, cfg=s)
    assert r.ok and r.leverage == 10 and r.qty == pytest.approx(0.011)
    assert abs(r.risk_actual - 2.0) <= 0.2
    # stop çok dar: 2 USDT risk için gereken teminat (nominal / 10) kasayı aşar
    r = compute_position_size(side=Side.LONG, equity=200, available_balance=5000, entry=entry, stop=entry - 5,
                              filters=F, cfg=s)
    assert not r.ok and r.reason == "insufficient_margin"
    # stop > %0,38
    r = compute_position_size(side=Side.SHORT, equity=200, available_balance=5000, entry=entry, stop=entry * 1.005,
                              filters=F, cfg=s)
    assert not r.ok and r.reason == "stop_too_wide"
    # borsa adımı yüzünden risk 2 USDT'den %10'dan fazla sapıyor
    coarse = SymbolFilters("BTCUSDT", Decimal("0.1"), Decimal("0.01"), Decimal("0.01"), Decimal("1000"),
                           Decimal("0.01"), Decimal("1000"), 100.0, 125, 0.004, 0.0)
    r = compute_position_size(side=Side.LONG, equity=200, available_balance=5000, entry=entry, stop=89_700.0,
                              filters=coarse, cfg=s)
    assert not r.ok and r.reason == "risk_not_exact"


def test_risk_engine_single_tp_and_one_position():
    s = scalp_settings()
    st = TradeSetup("BTCUSDT", Side.SHORT, 90_000.0, 90_180.0, 89_748.0, 89_748.0, 89_748.0, 0, 0)
    pf = PortfolioState(200, 5000, 200, [], 0, None, utc_now())
    d = evaluate_trade(Candidate(st, 0.00001, 1e7, {}), pf, TradingGate(True), F, s)
    assert d.allowed, d.reasons
    assert len(d.tp_levels) == 1 and d.tp_levels[0].price == pytest.approx(90_000 - 1.4 * 180, abs=0.1)
    from backend.smc.risk.engine import OpenPositionView
    pf.open_positions = [OpenPositionView("ETHUSDT", Side.LONG, 2.0)]
    d = evaluate_trade(Candidate(st, 0.00001, 1e7, {}), pf, TradingGate(True), F, s)
    assert not d.allowed and d.reasons == ["max_open_positions"]


# ------------------------------------------------------------------ pozisyon yönetimi
def _open(repo, notifier, filters, side=Side.LONG):
    tm, ex, s, safe = build(repo, notifier, filters, strategy="scalp", taker_fee=0.0, slippage_pct=0.0)
    entry, stop = 100.0, (99.8 if side is Side.LONG else 100.2)
    st = TradeSetup("TESTUSDT", side, entry, stop, 0, 0, 0, 0, 0)
    pf = PortfolioState(200, 5000, 200, [], 0, None, utc_now())
    d = evaluate_trade(Candidate(st, 0.00001, 1e7, {}), pf, TradingGate(True), filters, s)
    assert d.allowed, d.reasons
    pos = run(tm.open_position(st, d, None))
    assert pos is not None and len([o for o in repo.orders_for_position(pos.id) if o.purpose.startswith("TP")]) == 1
    return tm, ex, s, pos


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_breakeven_at_07r_moves_stop_to_entry_once(repo, notifier, filters, side):
    tm, ex, s, pos = _open(repo, notifier, filters, side)
    p = SC.ScalpParams.from_settings(s)
    sg = side.sign
    r = abs(pos.entry_price - pos.initial_stop)
    assert run(tm.scalp_check(pos, pos.entry_price + sg * 0.5 * r, [], p)) is None      # 0,5R: değişiklik yok
    assert run(tm.scalp_check(pos, pos.entry_price + sg * 0.71 * r, [], p)) == "BREAKEVEN"  # 0,7R
    pos = repo.get_position(pos.id)
    assert pos.be_active and pos.stop_price == pytest.approx(pos.entry_price)
    assert pos.state == PosState.BREAKEVEN_ACTIVE.value
    assert run(tm.scalp_check(pos, pos.entry_price + sg * r, [], p)) is None           # tekrar etmez
    assert sum(1 for o in repo.orders_for_position(pos.id) if o.purpose == "SL" and o.active) == 1


def test_time_exit_after_six_minutes(repo, notifier, filters):
    tm, ex, s, pos = _open(repo, notifier, filters)
    p = SC.ScalpParams.from_settings(s)
    repo.update_position(pos.id, entry_filled_at=utc_now() - timedelta(minutes=6, seconds=1))
    assert run(tm.scalp_check(repo.get_position(pos.id), 100.05, [], p)) == "TIME_EXIT"
    pos = repo.get_position(pos.id)
    assert pos.state == PosState.CLOSED.value and pos.exit_reason == "TIME_EXIT"
    assert "TESTUSDT" not in ex.positions_


def test_opposite_strong_candle_closes(repo, notifier, filters):
    tm, ex, s, pos = _open(repo, notifier, filters)
    p = SC.ScalpParams.from_settings(s)
    t_open = int(ensure_utc(pos.entry_filled_at).timestamp() * 1000)
    prev = [c(t_open - (16 - i) * M1, 100, 100.02, 99.98, 100.01, 100.0) for i in range(16)]
    weak = c(t_open + M1, 100, 100.01, 99.9, 99.95, 120.0)                     # hacim düşük
    assert run(tm.scalp_check(pos, 99.95, prev + [weak], p)) is None
    strong = c(t_open + M1, 100.0, 100.01, 99.85, 99.86, 400.0)                 # ters, gövde %88, hacim 4×
    assert run(tm.scalp_check(repo.get_position(pos.id), 99.86, prev + [strong], p)) == "OPPOSITE_CANDLE"
    assert repo.get_position(pos.id).exit_reason == "OPPOSITE_CANDLE"
