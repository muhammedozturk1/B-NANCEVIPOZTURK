"""4 saatlik trend takibi: kanal kırılımında giriş, iz süren stopla çıkış.

Kurallar (2023-2026 verisiyle test edildi, README'de sonuçlar):
- Giriş: 4s mum, önceki N mumun en yüksek seviyesinin üstünde kapanırsa LONG
  (en düşük seviyesinin altında kapanırsa SHORT). Giriş, mum kapanışının hemen ardından piyasa emriyle.
- Başlangıç stopu: giriş - K x ATR(14, 4s).
- Çıkış: hedef yok. Her 4s kapanışında stop, son M mumun en düşük seviyesine çekilir (sadece kâr yönünde).
- Korku & Açgözlülük: aşırı açgözlülükte (>=75) long, aşırı korkuda (<=25) short açılmaz.

Short için ayrı kod yok: mumlar aynalanır (fiyat -> -fiyat) ve aynı long kuralı çalışır.
"""
from __future__ import annotations

import math

import numpy as np

from .smc import Candles, atr
from .strategy import Signal, fmt_price, n

H4 = 14_400_000


def fng_label(v: int | None) -> str:
    if v is None:
        return "bilinmiyor"
    if v <= 25:
        return "aşırı korku"
    if v < 45:
        return "korku"
    if v <= 55:
        return "nötr"
    if v < 75:
        return "açgözlülük"
    return "aşırı açgözlülük"


def fng_blocks(side: str, fng: int | None, s: dict) -> str | None:
    if not s.get("fng_filter", True) or fng is None:
        return None
    if side == "LONG" and fng >= 75:
        return f"Aşırı açgözlülükte ({fng}) long açılmaz"
    if side == "SHORT" and fng <= 25:
        return f"Aşırı korkuda ({fng}) short açılmaz"
    return None


def trail_level(c: Candles, exit_n: int) -> float:
    """Long için iz süren stop seviyesi: son exit_n kapanmış mumun en düşüğü (aynalanmışta en yüksek)."""
    return float(c.low[-exit_n:].min())


def evaluate(symbol: str, c4: Candles, s: dict, fng: int | None) -> tuple[list[Signal], list[dict]]:
    """Son kapanmış 4s mumu değerlendirir. Dönüş: (açılacak sinyaller, panel için adaylar)."""
    N = int(s["trend_n"])
    k_atr = float(s["trend_stop_atr"])
    signals: list[Signal] = []
    cands: list[dict] = []
    if len(c4) < N + 20:
        return signals, cands
    t = len(c4) - 1
    for side, c in (("LONG", c4), ("SHORT", c4.mirror())):
        px = (lambda v: -v) if c.mirrored else (lambda v: v)  # noqa: E731
        a = float(atr(c)[t])
        if not np.isfinite(a) or a <= 0:
            continue
        level = float(c.high[t - N:t].max())
        close = float(c.close[t])
        dist_atr = (level - close) / a
        if close <= level:
            if dist_atr <= 0.5:  # kırılıma çok yakın: panelde göster
                cands.append({"symbol": symbol, "side": side, "status": f"Kırılıma yakın ({n(dist_atr)} ATR)",
                              "level": px(level), "close": px(close)})
            continue
        stop = close - k_atr * a
        stop_pct = (close - stop) / abs(close) * 100
        vol = c4.volume
        vr = float(vol[t] / vol[t - 20:t].mean()) if vol[t - 20:t].mean() > 0 else 0.0
        word = {"LONG": ("tepesini", "üstünde", "yukarı"), "SHORT": ("dibini", "altında", "aşağı")}[side]
        reasons = [
            {"ok": True, "pts": 0, "text": f"4 saatlik mum, son {N} mumun {word[0]} ({fmt_price(px(level))}) kırıp "
                                           f"{word[1]} kapandı ({fmt_price(px(close))}): {word[2]} yönlü trend başlangıcı"},
            {"ok": True, "pts": 0, "text": f"Başlangıç stopu {n(k_atr)}×ATR uzakta ({fmt_price(px(stop))}, %{n(stop_pct)}): "
                                           f"normal dalgalanmada patlamayacak kadar geniş"},
            {"ok": True, "pts": 0, "text": f"Hedef yok: stop her 4 saatte son {int(s['trend_exit_n'])} mumun "
                                           f"{'dibine' if side == 'LONG' else 'tepesine'} çekilir, trend sürdükçe pozisyon açık kalır"},
            {"ok": None, "pts": 0, "text": f"Korku & Açgözlülük endeksi: {fng if fng is not None else '—'} ({fng_label(fng)})"},
            {"ok": None, "pts": 0, "text": f"Kırılım mumunun hacmi 20 mumluk ortalamanın {n(vr)} katı (bilgi amaçlı)"},
        ]
        close_time = int(c4.time[t]) + H4
        sig = Signal(
            symbol=symbol, side=side, entry=px(close), stop=px(stop), tp1=None, tp2=None, rr=0.0,
            stop_pct=round(stop_pct, 3), score=None, structure_score=0.0, flow_score=0.0,
            setup_time=close_time, expiry_time=close_time + 15 * 60_000,
            key=f"{symbol}:{side}:trend:{int(c4.time[t])}", reasons=reasons,
            annotations={"tf": "4h", "strategy": "trend",
                         "breakout": {"price": px(level), "time": int(c4.time[t]), "from_time": int(c4.time[t - N])},
                         "init_stop": px(stop), "atr": a, "fng": fng, "volume_ratio": vr},
        )
        blocked = fng_blocks(side, fng, s)
        if blocked:
            cands.append({"symbol": symbol, "side": side, "status": f"Engellendi: {blocked}", "level": px(level),
                          "close": px(close), "reasons": [r["text"] for r in reasons[:2]]})
            continue
        signals.append(sig)
    return signals, cands


def new_trail_stop(t: dict, c4: Candles, exit_n: int) -> float | None:
    """Açık trend işlemi için yeni iz süren stop (sadece kâr yönünde iyileşirse döner)."""
    if len(c4) < exit_n:
        return None
    if t["side"] == "LONG":
        lvl = float(c4.low[-exit_n:].min())
        return lvl if lvl > (t["sl"] or -math.inf) else None
    lvl = float(c4.high[-exit_n:].max())
    return lvl if lvl < (t["sl"] or math.inf) else None


# ---------------------------------------------------------------- geçmiş test
def simulate(symbol: str, c4: Candles, s: dict, start_ms: int, fng_by_day: dict[int, int] | None = None) -> list[dict]:
    """Canlı kuralların aynısı: kapanışta giriş (sonraki açılış), 3xATR stop, iz süren stop, F&G engeli."""
    N, k_atr, exit_n = int(s["trend_n"]), float(s["trend_stop_atr"]), int(s["trend_exit_n"])
    fee, slip = s["taker_fee_pct"] / 100, s["slippage_pct"] / 100
    out: list[dict] = []
    for side, c in (("LONG", c4), ("SHORT", c4.mirror())):
        px = (lambda v: -v) if c.mirrored else (lambda v: v)  # noqa: E731
        a_arr = atr(c)
        i = N + 20
        while i < len(c) - 1:
            if c.time[i] < start_ms or not np.isfinite(a_arr[i]):
                i += 1
                continue
            level = c.high[i - N:i].max()
            if c.close[i] <= level:
                i += 1
                continue
            fng = None
            if fng_by_day:
                fng = fng_by_day.get(int(c.time[i] + H4) // 86_400_000)
            if fng_blocks(side, fng, s):
                i += 1
                continue
            e = c.open[i + 1] + abs(c.open[i + 1]) * slip
            stop = c.close[i] - k_atr * a_arr[i]
            risk = e - stop
            if risk <= 0:
                i += 1
                continue
            sl = stop
            k = i + 1
            ex = None
            mfe = mae = 0.0
            while k < len(c):
                mfe = max(mfe, (c.high[k] - e) / risk)
                mae = max(mae, (e - c.low[k]) / risk)
                if c.low[k] <= sl:
                    ex = sl - abs(sl) * slip
                    break
                if k >= exit_n:
                    sl = max(sl, c.low[k - exit_n + 1:k + 1].min())
                k += 1
            reason = "İz süren stop" if ex is not None and sl > e else "Stop"
            if ex is None:
                k = len(c) - 1
                ex = c.close[k]
                reason = "Test sonu (açık)"
            r = ((ex - e) - (abs(e) + abs(ex)) * fee) / risk
            out.append({
                "symbol": symbol, "side": side, "status": "closed", "score": None,
                "setup_time": int(c.time[i]) + H4, "entry_time": int(c.time[i + 1]), "exit_time": int(c.time[k]) + H4,
                "entry_price": px(e), "exit_price": px(ex), "sl": px(sl), "sl_initial": px(stop), "tp1": None, "tp2": None,
                "tp1_hit": 0, "r_multiple": r, "fee_r": (abs(e) + abs(ex)) * fee / risk, "exit_reason": reason,
                "mfe": mfe, "mae": mae,
                "reasons": [{"ok": True, "pts": 0, "text": f"4s kırılım: son {N} mum seviyesi {fmt_price(px(level))}"}],
                "annotations": {"tf": "4h", "strategy": "trend", "init_stop": px(stop),
                                "breakout": {"price": px(level), "time": int(c.time[i]), "from_time": int(c.time[i - N])}},
            })
            i = k + 1
    return out
