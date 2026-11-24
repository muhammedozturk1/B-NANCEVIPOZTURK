"""Hızlı giriş – yakın hedef scalp stratejisi (BTCUSDT, ETHUSDT; 5 dk yön, 1 dk giriş).

Kullanıcı şartnamesi (kurallar esnetilmez; sayısal karşılığı olmayan ifadeler ayarlarla tanımlıdır):
Yön (5 dk, kapanmış mumlar):
  - fiyat (son kapanmış 1 dk kapanışı) EMA20(5 dk) üstünde → sadece LONG, altında → sadece SHORT
  - fiyat EMA'ya `sc_ema_min_dist_pct`'den yakınsa ("çok yapışık") → işlem yok
Giriş (son kapanmış 1 dk mum = kırılım mumu; hepsi aynı anda):
  1. kapanış, önceki 6 mumun en yüksek (LONG) / en düşük (SHORT) seviyesini kırar
  2. hacim ≥ önceki 15 mumun ortalamasının 1,8 katı
  3. gövde ≥ mum boyunun %55'i (ve mum kırılım yönünde)
  4. RSI(5, 1 dk): LONG < 78, SHORT > 22
Çıkış:
  - stop: kırılım mumunun hemen altı/üstü (`sc_stop_buffer_pct`); giriş→stop mesafesi %0,38'i geçerse işlem yok
  - TP = 1,4 × stop mesafesi; 0,7R görülünce stop girişe; en fazla 6 dk; ters yönde güçlü hacimli mumda kapat
Ek filtreler: 01:30–05:30 UTC arası yeni işlem yok; spread normalin üstündeyse yok; son 2 mumda aşırı uzun fitil
(`sc_max_wick_ratio`: tek fitil mum boyunun bu oranından uzun) varsa yok.
Risk: her işlemde sabit 2 USDT; miktar = 2 / stop mesafesi; kaldıraç 10x; aynı anda en fazla 1 pozisyon.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.smc.core.types import Candle, Side
from backend.smc.strategy.signal import Component, SignalEvaluation, TradeSetup


@dataclass(frozen=True)
class ScalpParams:
    ema_period: int = 20
    ema_min_dist_pct: float = 0.0005      # %0,05: fiyat EMA'ya bundan yakınsa "yapışık"
    breakout_lookback: int = 6
    volume_lookback: int = 15
    volume_mult: float = 1.8
    min_body_ratio: float = 0.55
    rsi_period: int = 5
    rsi_long_max: float = 78.0
    rsi_short_min: float = 22.0
    stop_buffer_pct: float = 0.0001       # kırılım mumunun "hemen" altı/üstü: %0,01
    max_stop_pct: float = 0.0038
    tp_r: float = 1.4
    be_r: float = 0.7
    max_hold_min: int = 6
    max_wick_ratio: float = 0.60
    no_trade_start_min: int = 90          # 01:30 UTC
    no_trade_end_min: int = 330           # 05:30 UTC
    risk_usdt: float = 2.0
    leverage: int = 10

    @classmethod
    def from_settings(cls, s) -> "ScalpParams":
        return cls(
            ema_period=s.sc_ema_period, ema_min_dist_pct=s.sc_ema_min_dist_pct,
            breakout_lookback=s.sc_breakout_lookback, volume_lookback=s.sc_volume_lookback,
            volume_mult=s.sc_volume_mult, min_body_ratio=s.sc_min_body_ratio, rsi_period=s.sc_rsi_period,
            rsi_long_max=s.sc_rsi_long_max, rsi_short_min=s.sc_rsi_short_min, stop_buffer_pct=s.sc_stop_buffer_pct,
            max_stop_pct=s.sc_max_stop_pct, tp_r=s.sc_tp_r, be_r=s.sc_be_r, max_hold_min=s.sc_max_hold_min,
            max_wick_ratio=s.sc_max_wick_ratio, no_trade_start_min=s.sc_no_trade_start_min,
            no_trade_end_min=s.sc_no_trade_end_min, risk_usdt=s.sc_risk_usdt, leverage=s.max_leverage)


# ------------------------------------------------------------------ göstergeler
def ema(values: Sequence[float], n: int) -> float | None:
    """Standart EMA (ilk n değerin SMA'sıyla başlar)."""
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], n: int = 5) -> float | None:
    """Wilder RSI; en az 3n kapanışla kararlı."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def body_ratio(c: Candle) -> float:
    rng = c.high - c.low
    return abs(c.close - c.open) / rng if rng > 0 else 0.0


def max_wick_ratio(c: Candle) -> float:
    rng = c.high - c.low
    if rng <= 0:
        return 0.0
    return max(c.high - max(c.open, c.close), min(c.open, c.close) - c.low) / rng


def in_no_trade_window(ms: int, p: ScalpParams) -> bool:
    m = (ms // 60_000) % 1440
    return p.no_trade_start_min <= m < p.no_trade_end_min


def strong_opposite(c: Candle, prev: Sequence[Candle], side: Side, p: ScalpParams) -> bool:
    """Ters yönde güçlü hacimli mum: yön ters, gövde ≥ %55, hacim ≥ 1,8 × önceki 15 mum ortalaması."""
    if len(prev) < p.volume_lookback:
        return False
    bearish = c.close < c.open
    if (side is Side.LONG) != bearish:
        return False
    avg = sum(x.volume for x in prev[-p.volume_lookback:]) / p.volume_lookback
    return avg > 0 and c.volume >= p.volume_mult * avg and body_ratio(c) >= p.min_body_ratio


# ------------------------------------------------------------------ değerlendirme
def evaluate_scalp(symbol: str, c5: Sequence[Candle], c1: Sequence[Candle], p: ScalpParams,
                   spread_pct: float | None = None, max_spread_pct: float | None = None) -> SignalEvaluation | None:
    """Son KAPANMIŞ 1 dk mumu değerlendirir. Kırılım yoksa None. Kırılım varsa tüm kontrollerin sonucunu
    döner; hepsi geçerse `setup` dolu (giriş = son kapanış, gerçek giriş dolum fiyatından yeniden hesaplanır)."""
    need = max(p.breakout_lookback, p.volume_lookback) + 1
    if len(c1) < max(need, 3 * p.rsi_period + 1) or len(c5) < p.ema_period:
        return None
    b = c1[-1]
    prior = c1[-1 - p.breakout_lookback:-1]
    hi, lo = max(c.high for c in prior), min(c.low for c in prior)
    if b.close > hi:
        side, level = Side.LONG, hi
    elif b.close < lo:
        side, level = Side.SHORT, lo
    else:
        return None
    sgn = 1 if side is Side.LONG else -1
    ev = SignalEvaluation(symbol, side, b.open_time, score=0)
    reasons: list[str] = []

    def check(name: str, ok: bool, text: str, key: str) -> None:
        ev.add(Component(name, 0, 0, ok, True, {"text": text}))
        if not ok:
            reasons.append(key)

    # 5 dk yön
    e = ema([c.close for c in c5], p.ema_period)
    dist = (b.close - e) / e if e else 0.0
    trend_ok = e is not None and dist * sgn > 0
    sticky = e is not None and abs(dist) < p.ema_min_dist_pct
    check("yon_5dk", trend_ok and not sticky,
          f"5 dk EMA{p.ema_period} {e:g}, fiyat %{dist * 100:+.3f} uzakta" +
          (" (EMA'ya yapışık)" if sticky else "" if trend_ok else " (ters yön)"),
          "trend:sticky" if sticky else "trend:against")
    word = "tepesini" if side is Side.LONG else "dibini"
    check("kirilim", True, f"1 dk mum son {p.breakout_lookback} mumun {word} ({level:g}) kırdı, kapanış {b.close:g}",
          "breakout")
    vols = [c.volume for c in c1[-1 - p.volume_lookback:-1]]
    vavg = sum(vols) / len(vols)
    vr = b.volume / vavg if vavg > 0 else 0.0
    check("hacim", vr >= p.volume_mult, f"hacim ortalamanın {vr:.2f} katı (en az {p.volume_mult:g})", "volume")
    br = body_ratio(b)
    dir_ok = (b.close - b.open) * sgn > 0
    check("govde", br >= p.min_body_ratio and dir_ok,
          f"gövde mum boyunun %{br * 100:.0f}'i (en az %{p.min_body_ratio * 100:.0f})" + ("" if dir_ok else ", ters renk"),
          "body")
    r = rsi([c.close for c in c1[-(3 * p.rsi_period + 30):]], p.rsi_period)
    rsi_ok = r is not None and (r < p.rsi_long_max if side is Side.LONG else r > p.rsi_short_min)
    check("rsi", rsi_ok, f"RSI({p.rsi_period}) {r:.1f}" if r is not None else "RSI yok", "rsi")
    wick = max(max_wick_ratio(c) for c in c1[-2:])
    check("fitil", wick <= p.max_wick_ratio, f"son 2 mumda en uzun fitil boyun %{wick * 100:.0f}'i "
                                             f"(en fazla %{p.max_wick_ratio * 100:.0f})", "wick")
    closed_ms = b.close_time + 1
    check("saat", not in_no_trade_window(closed_ms, p), "01:30–05:30 UTC işlem yok" if in_no_trade_window(closed_ms, p)
          else "saat uygun", "session")
    if spread_pct is not None and max_spread_pct is not None:
        check("spread", spread_pct <= max_spread_pct, f"spread %{spread_pct * 100:.4f} (en fazla %{max_spread_pct * 100:.3f})",
              "spread")
    stop = (b.low * (1 - p.stop_buffer_pct)) if side is Side.LONG else (b.high * (1 + p.stop_buffer_pct))
    stop_pct = abs(b.close - stop) / b.close
    check("stop_mesafesi", 0 < stop_pct <= p.max_stop_pct,
          f"stop {stop:g} (%{stop_pct * 100:.3f}, en fazla %{p.max_stop_pct * 100:.2f})", "stop_distance")
    ev.reasons = reasons
    ev.snapshot = {"strategy": "scalp", "ema5": e, "rsi": r, "volume_ratio": vr, "body_ratio": br, "level": level,
                   "planned": {"entry": b.close, "stop": stop, "stop_pct": stop_pct}}
    if not reasons:
        tp = b.close + sgn * p.tp_r * abs(b.close - stop)
        ev.setup = TradeSetup(symbol, side, b.close, stop, tp, tp, tp, 0, b.open_time)
    return ev


def plan_from_fill(side: Side, fill: float, stop: float, p: ScalpParams, equity: float) -> dict | None:
    """Gerçek dolum fiyatına göre stop mesafesi / miktar / TP. Kurallar: risk tam `risk_usdt`, stop mesafesi
    ≤ %0,38, gereken teminat (nominal / kaldıraç) ≤ özsermaye. Sağlanamazsa None (işlem açılmaz)."""
    sgn = 1 if side is Side.LONG else -1
    dist = (fill - stop) * sgn
    if dist <= 0:
        return None
    pct = dist / fill
    if pct > p.max_stop_pct:
        return None
    qty = p.risk_usdt / dist
    notional = qty * fill
    if notional / p.leverage > equity:
        return None
    return {"qty": qty, "notional": notional, "stop_pct": pct, "tp": fill + sgn * p.tp_r * dist,
            "be_trigger": fill + sgn * p.be_r * dist, "risk": p.risk_usdt}
