"""4 saatlik kanal kırılımı + sabit stop/hedef (kasanın %1'i / %2'si).

Neden bu strateji (araştırma, README'de tablo):
- 1 dakikalık scalp kuralları (momentum, kırılım, RSI dönüşü, likidite süpürme) 12 aylık 1dk veride
  komisyon+kayma sonrası rastgele girişten farksız ve zararlı çıktı; 1 saatlik ve 15 dakikalık trend takibi de
  doğrulama döneminde zarar etti.
- 4 saatlik kırılım girişi + net 2R sabit hedef (stop = kasanın %1'i, hedef = kasanın %2'si, komisyon dahil)
  2023, 2024, 2025 ve 2026'nın her birinde artıda kaldı (%1 risk, en fazla 3 pozisyon).

Kurallar:
- Son kapanmış 4s mum, önceki N mumun en yüksek seviyesinin üstünde kapanırsa LONG, en düşüğünün altında
  kapanırsa SHORT.
- Stop: kapanış -/+ K x ATR(14, 4s). Pozisyon büyüklüğü, stop olursa kasanın %1'i kaybedilecek şekilde
  (komisyon ve kayma dahil) hesaplanır.
- Hedef: tek TP; net kâr = 2 x net kayıp (risk/plan.py: tp_prices).
- Korku & Açgözlülük: >= 75'te long, <= 25'te short açılmaz.
"""
from __future__ import annotations

from typing import Sequence

from backend.smc.config import Settings
from backend.smc.core.types import Candle, Side
from backend.smc.strategy.signal import Component, SignalEvaluation, TradeSetup


def atr(candles: Sequence[Candle], n: int = 14) -> float | None:
    """Wilder ATR (ana bottaki backend/smcbot/smc.py ile aynı formül); son değeri döner."""
    if len(candles) < n + 1:
        return None
    trs = []
    prev = candles[0].close
    for c in candles:
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
        prev = c.close
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def fng_block(side: Side, fng: int | None, s: Settings) -> str | None:
    if not s.bo_fng_filter or fng is None:
        return None
    if side is Side.LONG and fng >= 75:
        return f"aşırı açgözlülükte ({fng}) long açılmaz"
    if side is Side.SHORT and fng <= 25:
        return f"aşırı korkuda ({fng}) short açılmaz"
    return None


def evaluate_breakout(symbol: str, c4: Sequence[Candle], fng: int | None, s: Settings) -> list[SignalEvaluation]:
    """Son kapanmış 4s mumu değerlendirir. Sadece kırılım olan yönler için değerlendirme döner."""
    n = int(s.bo_channel)
    if len(c4) < n + 20:
        return []
    last, prior = c4[-1], c4[-n - 1:-1]
    a = atr(c4)
    if a is None or a <= 0:
        return []
    hi, lo = max(c.high for c in prior), min(c.low for c in prior)
    out = []
    for side, broke, level in ((Side.LONG, last.close > hi, hi), (Side.SHORT, last.close < lo, lo)):
        if not broke:
            continue
        sgn = 1 if side is Side.LONG else -1
        entry = last.close
        stop = entry - sgn * s.bo_stop_atr * a
        stop_pct = abs(entry - stop) / entry
        ev = SignalEvaluation(symbol, side, last.open_time, score=0)
        word = "tepesini" if side is Side.LONG else "dibini"
        ev.add(Component("kanal_kirilimi", 0, 0, True, True,
                         {"text": f"4s mum son {n} mumun {word} ({level:g}) kırıp {entry:g} seviyesinde kapandı",
                          "level": level, "close": entry}))
        ev.add(Component("atr_stop", 0, 0, True, True,
                         {"text": f"Stop {s.bo_stop_atr:g}×ATR uzakta ({stop:g}, %{stop_pct * 100:.2f}); "
                                  f"stop olursa kayıp kasanın %{s.bo_risk_per_trade * 100:g}'i", "atr": a}))
        ev.add(Component("hedef", 0, 0, True, True,
                         {"text": f"Tek hedef: net kâr kasanın %{s.bo_risk_per_trade * s.bo_target_r * 100:g}'i "
                                  f"({s.bo_target_r:g}R, komisyon sonrası)"}))
        blocked = fng_block(side, fng, s)
        ev.add(Component("korku_endeksi", 0, 0, blocked is None, False,
                         {"text": f"Korku & Açgözlülük: {fng if fng is not None else '—'}" + (f" — {blocked}" if blocked else "")}))
        ev.snapshot = {"strategy": "breakout", "atr": a, "level": level, "fear_greed": fng,
                       "planned": {"entry": entry, "stop": stop, "stop_pct": stop_pct}}
        if blocked:
            ev.reasons.append(f"fng:{blocked}")
        elif not (s.bo_min_stop_pct <= stop_pct <= s.bo_max_stop_pct):
            ev.reasons.append(f"stop_distance:{stop_pct:.4f}")
        else:
            # tp1..3 sadece gösterim içindir; gerçek hedef dolum fiyatından plan.tp_prices ile hesaplanır
            ev.setup = TradeSetup(symbol, side, entry, stop, entry + sgn * 2 * abs(entry - stop),
                                  entry + sgn * 2 * abs(entry - stop), entry + sgn * 2 * abs(entry - stop),
                                  0, last.open_time)
        out.append(ev)
    return out
