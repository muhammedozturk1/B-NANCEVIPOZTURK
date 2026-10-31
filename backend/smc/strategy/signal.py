"""Signal Engine: deterministik SMC/ICT pipeline + 15 puanlık skor.

Pipeline: 15m Structure → 5m Structure → Liquidity Sweep → BOS/CHoCH → Displacement →
OTE → FVG → CVD → Volume → OI → Funding → Macro → 3m Trigger → Score.

Zorunlu (mandatory) bileşenler: 15m structure, 5m BOS/CHoCH, liquidity sweep, OTE ve 3m trigger.
Herhangi biri başarısızsa skor ne olursa olsun sinyal reddedilir.
Strateji motoru yalnızca kapanmış mumlarla çalışır. Look-ahead yoktur.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Sequence

from backend.smc.config import Settings
from backend.smc.core.types import Candle, Side
from backend.smc.strategy.liquidity import (detect_sweep, equal_levels, previous_day_levels, previous_session_levels,
                                    swing_levels)
from backend.smc.strategy.orderflow import cvd_bias, recent_delta, synthetic_cvd
from backend.smc.strategy.patterns import average_volume, compute_ote, find_displacement, find_fvgs
from backend.smc.strategy.structure import Direction, analyze_structure, atr

MAX_SCORE = 15


@dataclass
class MarketContext:
    oi_change: tuple[float, float] | None = None  # (oi_change_pct, price_change_pct)
    funding_rate: float | None = None
    fear_greed: int | None = None
    spread_pct: float | None = None
    depth_usdt: float | None = None


@dataclass
class Component:
    name: str
    points: int
    max_points: int
    passed: bool
    mandatory: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class TradeSetup:
    symbol: str
    side: Side
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    candle_time: int
    risk_reduce: bool = False


@dataclass
class SignalEvaluation:
    symbol: str
    side: Side
    candle_time: int
    score: int = 0
    components: list[Component] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    setup: TradeSetup | None = None
    snapshot: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.setup is not None

    @property
    def structural(self) -> bool:
        """Sweep + BOS/CHoCH mevcut mu (loglamaya değer mi)."""
        d = {c.name: c for c in self.components}
        return d.get("liquidity_sweep", Component("", 0, 0, False)).passed and \
            d.get("ltf_structure", Component("", 0, 0, False)).passed

    def add(self, comp: Component) -> Component:
        self.components.append(comp)
        return comp


def score_components(components: Sequence[Component]) -> int:
    return sum(min(c.points, c.max_points) for c in components)


def _sign(direction: Direction) -> int:
    return 1 if direction == "bullish" else -1


def evaluate_direction(symbol: str, direction: Direction, c15: Sequence[Candle], c5: Sequence[Candle],
                       c3: Sequence[Candle], ctx: MarketContext, s: Settings) -> SignalEvaluation:
    side = Side.from_direction(direction)
    ev = SignalEvaluation(symbol, side, c3[-1].open_time if c3 else 0)
    if len(c15) < 50 or len(c5) < 100 or len(c3) < 30:
        ev.reasons.append("insufficient_data")
        return ev
    sgn = _sign(direction)
    snap: dict = {"direction": direction}
    ev.snapshot = snap

    # 1) 15m HTF structure
    st15 = analyze_structure(c15, s.pivot_left, s.pivot_right)
    htf_ok = st15.trend == direction
    ev.add(Component("htf_structure", 2 if htf_ok else 0, 2, htf_ok, True, {"trend": st15.trend}))
    snap["structure_15m"] = st15.trend

    # 2) 5m structure + likidite seviyeleri
    st5 = analyze_structure(c5, s.pivot_left, s.pivot_right)
    snap["structure_5m"] = st5.trend
    levels = (swing_levels(st5.swings, c5, external=False) + swing_levels(st15.swings, c15, external=True)
              + equal_levels(st5.swings, c5, s.equal_level_tolerance) + previous_day_levels(c15)
              + previous_session_levels(c5, s.sessions))

    # 3) Liquidity sweep (+ karşı yönde structure confirmation)
    sweep = detect_sweep(c5, levels, st5, direction, s.sweep_lookback, s.sweep_reclaim_bars)
    if sweep is None:
        ev.add(Component("liquidity_sweep", 0, 2, False, True))
        ev.add(Component("ltf_structure", 0, 2, False, True))
        ev.reasons.append("no_sweep")
        ev.score = score_components(ev.components)
        return ev
    ev.add(Component("liquidity_sweep", 2 if sweep.level.strength >= 2 else 1, 2, True, True,
                     {"level": sweep.level.price, "kind": sweep.level.kind, "extreme": sweep.extreme}))
    snap["liquidity_sweep"] = {"time": sweep.time, "level": sweep.level.price, "kind": sweep.level.kind,
                               "extreme": sweep.extreme}
    conf = sweep.confirm
    ev.add(Component("ltf_structure", 2, 2, True, True, {"event": conf.kind, "level": conf.level}))
    snap["bos" if conf.kind == "BOS" else "choch"] = {"time": conf.time, "level": conf.level}
    snap["ltf_event"] = {"kind": conf.kind, "time": conf.time, "level": conf.level}

    # 4) Impulse leg + displacement
    n5 = len(c5)
    seg = range(sweep.index, n5)
    if direction == "bullish":
        leg_end_idx = max(seg, key=lambda i: c5[i].high)
        leg_end = c5[leg_end_idx].high
    else:
        leg_end_idx = min(seg, key=lambda i: c5[i].low)
        leg_end = c5[leg_end_idx].low
    leg_start = sweep.extreme
    atr5 = atr(c5, s.atr_period)
    leg_range = abs(leg_end - leg_start)
    impulsive = leg_end_idx >= conf.index and atr5 > 0 and leg_range >= s.min_leg_atr * atr5
    disp_idx = find_displacement(c5, sweep.index, leg_end_idx, direction, body_mult=s.displacement_body_mult,
                                 period=s.body_avg_period, close_location=s.displacement_close_location)
    ev.add(Component("displacement", 1 if disp_idx is not None else 0, 1, disp_idx is not None, False,
                     {"index_time": c5[disp_idx].open_time if disp_idx is not None else None}))
    snap["displacement"] = disp_idx is not None

    # 5) OTE (yalnızca valid impulse sonrası)
    ote = None
    ote_ok = False
    if impulsive:
        ote = compute_ote(direction, leg_start, leg_end, s.ote_low, s.ote_high, s.ote_preferred)
        leg_end_time = c5[leg_end_idx].open_time
        after_leg = [c for c in c3 if c.open_time >= leg_end_time]
        recent = after_leg[-s.confirm_lookback_3m:]
        touched = any(ote.touched_by(c) for c in recent)
        invalidated = any((c.close < leg_start) if direction == "bullish" else (c.close > leg_start) for c in after_leg)
        ote_ok = touched and not invalidated
        snap["ote"] = {"low": ote.low, "high": ote.high, "preferred": ote.preferred, "leg_start": leg_start,
                       "leg_end": leg_end, "start_time": sweep.time, "end_time": leg_end_time}
    ev.add(Component("ote", 1 if ote_ok else 0, 1, ote_ok, True,
                     {"impulsive": impulsive, "leg_range_atr": leg_range / atr5 if atr5 else None}))

    # 6) FVG
    fvgs = find_fvgs(c5, sweep.index, leg_end_idx, direction)
    ev.add(Component("fvg", 1 if fvgs else 0, 1, bool(fvgs), False, {"count": len(fvgs)}))
    if fvgs:
        f = fvgs[-1]
        snap["fvg"] = {"time": f.time, "low": f.low, "high": f.high}

    # 7) Synthetic CVD (5m eğim + 3m son delta)
    bias5 = cvd_bias(c5, s.cvd_window, s.cvd_slope_lookback)
    d3 = recent_delta(c3, 3)
    cvd_pts = (1 if bias5 * sgn > 0 else 0) + (1 if d3 * sgn > 0 else 0)
    ev.add(Component("cvd", cvd_pts, 2, cvd_pts > 0, False, {"cvd5_bias": bias5, "delta3m": d3}))
    snap["cvd"] = {"bias5": bias5, "delta3m": d3, "cvd3_last": synthetic_cvd(c3, s.cvd_window)[-1]}

    # 8) Volume (kırılım veya displacement mumunda)
    vidx = conf.index
    vol_ok = c5[vidx].volume >= s.volume_mult * average_volume(c5, vidx, s.volume_avg_period)
    if not vol_ok and disp_idx is not None:
        vol_ok = c5[disp_idx].volume >= s.volume_mult * average_volume(c5, disp_idx, s.volume_avg_period)
    ev.add(Component("volume", 1 if vol_ok else 0, 1, vol_ok))

    # 9) Open Interest (yalnızca bağlam)
    oi_ok = False
    if ctx.oi_change is not None:
        oi_chg, px_chg = ctx.oi_change
        oi_ok = oi_chg > s.oi_change_threshold and px_chg * sgn > 0
    ev.add(Component("oi", 1 if oi_ok else 0, 1, oi_ok, False, {"oi_change": ctx.oi_change}))
    snap["oi"] = ctx.oi_change

    # 10) Funding (yalnızca bağlam filtresi)
    risk_reduce = False
    if ctx.funding_rate is None:
        f_ok = False
    else:
        against = ctx.funding_rate * sgn  # long için pozitif funding = kalabalık long
        f_ok = against <= s.funding_extreme
        risk_reduce = not f_ok
    ev.add(Component("funding", 1 if f_ok else 0, 1, f_ok, False, {"rate": ctx.funding_rate}))
    snap["funding"] = ctx.funding_rate

    # 11) Makro sentiment (Fear & Greed) — "Fear=Short, Greed=Long" kuralı YOK.
    # Sadece trade yönünde aşırı kalabalıklaşmış sentimentte puan verilmez.
    if ctx.fear_greed is None:
        m_pts = max(0, min(1, s.fng_fallback_points))
    elif direction == "bullish":
        m_pts = 1 if ctx.fear_greed < s.fng_extreme_greed else 0
    else:
        m_pts = 1 if ctx.fear_greed > s.fng_extreme_fear else 0
    ev.add(Component("macro", m_pts, 1, m_pts > 0, False, {"fear_greed": ctx.fear_greed}))
    snap["macro"] = ctx.fear_greed

    # 12) 3m final trigger (micro structure + delta)
    last, p1, p2 = c3[-1], c3[-2], c3[-3]
    if direction == "bullish":
        trig = last.bullish and last.close > max(p1.high, p2.high) and last.delta > 0 and last.close < leg_end
    else:
        trig = last.bearish and last.close < min(p1.low, p2.low) and last.delta < 0 and last.close > leg_end
    trig = trig and ote_ok
    ev.add(Component("trigger_3m", 0, 0, trig, True, {"close": last.close, "delta": last.delta}))
    snap["trigger_3m"] = {"time": last.open_time, "close": last.close, "passed": trig}

    ev.score = score_components(ev.components)
    snap["score"] = ev.score
    snap["spread"] = ctx.spread_pct

    failed = [c.name for c in ev.components if c.mandatory and not c.passed]
    if failed:
        ev.reasons.append("mandatory_failed:" + ",".join(failed))
    if ev.score < s.min_score:
        ev.reasons.append(f"score_below_min:{ev.score}<{s.min_score}")

    # 13) SL / TP
    entry = last.close
    stop = leg_start - sgn * s.sl_buffer_atr * atr5
    risk = (entry - stop) * sgn
    if risk <= 0:
        ev.reasons.append("invalid_stop")
    else:
        stop_pct = risk / entry
        if stop_pct < s.min_stop_pct:
            ev.reasons.append(f"stop_too_tight:{stop_pct:.4%}")
        elif stop_pct > s.max_stop_pct:
            ev.reasons.append(f"stop_too_wide:{stop_pct:.4%}")
    snap["planned"] = {"entry": entry, "stop": stop}

    if not ev.reasons:
        r = risk
        ev.setup = TradeSetup(symbol, side, entry, stop, entry + sgn * s.tp1_r * r, entry + sgn * s.tp2_r * r,
                              entry + sgn * s.tp3_r * r, ev.score, last.open_time, risk_reduce)
        snap["planned"].update(tp1=ev.setup.tp1, tp2=ev.setup.tp2, tp3=ev.setup.tp3)
    return ev


def evaluate_symbol(symbol: str, c15: Sequence[Candle], c5: Sequence[Candle], c3: Sequence[Candle],
                    ctx: MarketContext, s: Settings) -> list[SignalEvaluation]:
    return [evaluate_direction(symbol, d, c15, c5, c3, ctx, s) for d in ("bullish", "bearish")]


def components_as_dicts(ev: SignalEvaluation) -> list[dict]:
    return [asdict(c) for c in ev.components]
