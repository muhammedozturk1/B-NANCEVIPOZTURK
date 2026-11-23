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

from . import exits as X
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


def fng_effective(fng: int | None, s: dict) -> tuple[int | None, str | None]:
    """F&G değeri alınamadıysa ayara göre davran: allow (filtre yok) | skip (işlem açma) | neutral_fallback (50 say)."""
    if fng is not None:
        return fng, None
    mode = s.get("fng_missing", "allow")
    if mode == "skip":
        return None, "Korku endeksi alınamadı (ayar: veri yoksa işlem açma)"
    if mode == "neutral_fallback":
        return 50, None
    return None, None


def fng_blocks(side: str, fng: int | None, s: dict) -> str | None:
    if not s.get("fng_filter", True):
        return None
    v, why = fng_effective(fng, s)
    if why:
        return why
    if v is None:
        return None
    hi_long, lo_long = float(s.get("fng_long_max", 75)), float(s.get("fng_long_min", 0))
    lo_short, hi_short = float(s.get("fng_short_min", 25)), float(s.get("fng_short_max", 100))
    if side == "LONG" and v >= hi_long:
        return f"Aşırı açgözlülükte ({v}) long açılmaz"
    if side == "LONG" and v < lo_long:
        return f"Korku endeksi {v} < {lo_long:g}: long açılmaz (sahte kırılım riski)"
    if side == "SHORT" and v <= lo_short:
        return f"Aşırı korkuda ({v}) short açılmaz"
    if side == "SHORT" and v > hi_short:
        return f"Korku endeksi {v} > {hi_short:g}: short açılmaz"
    return None


def exit_summary(s: dict) -> str:
    """Çıkış ayarlarının panelde okunacak özeti."""
    try:
        cfg = X.ExitConfig.from_settings(s)
    except ValueError:
        return "Çıkış ayarları hatalı"
    parts = []
    if cfg.lock_tiers:
        parts.append("kâr kilidi " + ", ".join(f"+{a:g}R→{b:+g}R" for a, b in cfg.lock_tiers))
    if cfg.partial_tiers:
        parts.append("kısmi TP " + ", ".join(f"+{a:g}R'de %{b * 100:.0f}" for a, b in cfg.partial_tiers))
    tm = {"off": "trailing kapalı", "swing": f"swing trailing (son {cfg.trail_swing_n} mum)",
          "atr": f"ATR trailing ({cfg.trail_atr_mult:g}×)", "both": f"swing + {cfg.trail_atr_mult:g}×ATR trailing"}[cfg.trail_mode]
    if cfg.trail_mode != "off" and cfg.trail_activate_r > -1:
        tm += f", +{cfg.trail_activate_r:g}R sonrası"
    parts.append(tm + f", {cfg.manage_tf} kapanışlarında")
    return "Hedef yok; çıkış: " + "; ".join(parts) + f" (tetik: {cfg.trigger_source})"


def bar_metrics(c: Candles, t: int) -> dict:
    """Kırılım mumunun ölçüleri (aynalanmış mumda short için de doğru yönde)."""
    o, h, lo, cl = float(c.open[t]), float(c.high[t]), float(c.low[t]), float(c.close[t])
    rng = h - lo
    base = c.volume[max(0, t - 20):t]
    vr = float(c.volume[t] / base.mean()) if len(base) and base.mean() > 0 else 0.0
    return {"volume_ratio": vr,
            "body_ratio": abs(cl - o) / rng if rng > 0 else 0.0,
            "close_strength": (cl - lo) / rng if rng > 0 else 0.0}


def regime_block(side: str, c: Candles, t: int, s: dict, fng: int | None) -> str | None:
    """Rejim filtreleri; her biri ayardan ayrı ayrı açılıp kapanır. Engel varsa nedenini döner."""
    why = fng_blocks(side, fng, s)
    if why:
        return why
    m = bar_metrics(c, t)
    if s.get("filter_volume") and m["volume_ratio"] < float(s.get("min_volume_ratio", 1.2)):
        return f"Hacim zayıf ({m['volume_ratio']:.2f}× < {float(s.get('min_volume_ratio', 1.2)):g}×)"
    if s.get("filter_body") and m["body_ratio"] < float(s.get("min_body_ratio", 0.5)):
        return f"Mum gövdesi küçük ({m['body_ratio']:.2f} < {float(s.get('min_body_ratio', 0.5)):g})"
    if s.get("filter_close_strength") and m["close_strength"] < float(s.get("min_close_strength", 0.6)):
        return f"Kapanış zayıf (mumun {m['close_strength']:.2f} seviyesinde < {float(s.get('min_close_strength', 0.6)):g})"
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
            {"ok": True, "pts": 0, "text": exit_summary(s)},
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
        sig.annotations.update(bar_metrics(c, t))
        blocked = regime_block(side, c, t, s, fng)
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
def _funding_arrays(funding) -> tuple[np.ndarray, np.ndarray]:
    if funding is None:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    if isinstance(funding, dict):
        ks = sorted(funding)
        return np.asarray(ks, dtype=np.int64), np.asarray([funding[k] for k in ks], dtype=float)
    t, r = funding
    return np.asarray(t, dtype=np.int64), np.asarray(r, dtype=float)


def simulate(symbol: str, c4: Candles, s: dict, start_ms: int, fng_by_day: dict[int, int] | None = None,
             c1: Candles | None = None, funding=None, exit_cfg: X.ExitConfig | None = None) -> list[dict]:
    """Canlı kuralların aynısı (exits.py ortak motor). Look-ahead yok:
    - sinyal 4s KAPANIŞINDA, giriş bir sonraki mumun açılışında (+kayma)
    - trailing yalnızca kapanmış mumlarla, bir sonraki mumdan itibaren geçerli
    Aynı mumda kötü sıra varsayımı:
    - önce stop kontrol edilir (mum stopun ötesinde açıldıysa çıkış AÇILIŞTAN); stop olduysa o mumda kâr kilidi/TP yok
    - mum içi tetikte (mark/last) önce tepe görülür, ardından dip yeni kilit stopuna değiyorsa çıkış kilit stopundan
    Maliyet: giriş, her kısmi ve son çıkışta taker komisyonu + kayma; gerçek funding (varsa).
    c1: daha ince mumlar (ör. 1s). Verilmezse 4s mumlar kullanılır (mum içi sıra daha belirsiz)."""
    N, k_atr = int(s["trend_n"]), float(s["trend_stop_atr"])
    cfg = exit_cfg or X.ExitConfig.from_settings(s)
    fee, slip = s["taker_fee_pct"] / 100, s["slippage_pct"] / 100
    manage_ms = {"1h": 3_600_000, "4h": H4}[cfg.manage_tf]
    f_t, f_r = _funding_arrays(funding)
    intrabar = cfg.trigger_source != "candle_close"
    out: list[dict] = []
    for side, c, sub in (("LONG", c4, c1 or c4), ("SHORT", c4.mirror(), (c1 or c4).mirror())):
        d_real = 1 if side == "LONG" else -1
        px = (lambda v: -v) if c.mirrored else (lambda v: v)  # noqa: E731
        a_arr = atr(c)
        close4 = c.time + H4
        sub_close = sub.time + sub.interval_ms
        i = N + 20
        while i < len(c) - 1:
            if c.time[i] < start_ms or not np.isfinite(a_arr[i]):
                i += 1
                continue
            level = c.high[i - N:i].max()
            if c.close[i] <= level:
                i += 1
                continue
            fng = fng_by_day.get(int(c.time[i] + H4) // 86_400_000) if fng_by_day else None
            if regime_block(side, c, i, s, fng):
                i += 1
                continue
            j = int(np.searchsorted(sub.time, close4[i]))
            if j >= len(sub) or sub.time[j] - close4[i] > sub.interval_ms:
                i += 1
                continue
            j0 = j
            e = sub.open[j] + abs(sub.open[j]) * slip
            stop = c.close[i] - k_atr * a_arr[i]
            if e - stop <= 0:
                i += 1
                continue
            st = X.ExitState.new("LONG", float(e), float(stop))  # aynalanmış uzayda her şey LONG
            frac = 1.0
            fills: list[tuple[float, float, str]] = []           # (oran, fiyat, neden)
            fund_r = 0.0
            mfe = mae = 0.0
            ex = None
            reason = ""
            moves = 0
            while j < len(sub):
                o, h, lo, cl = sub.open[j], sub.high[j], sub.low[j], sub.close[j]
                mfe = max(mfe, (h - e) / st.one_r)
                mae = max(mae, (e - lo) / st.one_r)
                # 1) önce stop (kötü sıra)
                if o <= st.stop:
                    ex, reason = o - abs(o) * slip, "gap"
                elif lo <= st.stop:
                    ex, reason = st.stop - abs(st.stop) * slip, "stop"
                if ex is not None:
                    break
                # 2) mum içi tetik (mark/last): tepe önce, sonra dip
                if intrabar:
                    plan = X.on_trigger(st, cfg, float(h))
                    for idx, fr in plan.partials:
                        trig_px = st.price_at(cfg.partial_tiers[idx][0])
                        fp = max(o, trig_px)
                        fills.append((fr, fp - abs(fp) * slip, f"TP{idx + 1}"))
                        frac -= fr
                        st.partials_done.append(idx)
                    if plan.stop is not None:
                        X.apply_stop(st, plan.stop, plan.stop_source)
                        moves += 1
                        if lo <= st.stop:
                            ex, reason = st.stop - abs(st.stop) * slip, "lock_same_bar"
                            break
                    if frac <= 1e-9:
                        ex, reason = fills[-1][1], "all_partial"
                        break
                t_close = int(sub_close[j])
                if t_close % manage_ms == 0:
                    # 3) mum kapanışı tetiği
                    if not intrabar:
                        plan = X.on_trigger(st, cfg, float(cl))
                        for idx, fr in plan.partials:
                            fills.append((fr, cl - abs(cl) * slip, f"TP{idx + 1}"))
                            frac -= fr
                            st.partials_done.append(idx)
                        if frac <= 1e-9:
                            ex, reason = fills[-1][1], "all_partial"
                            break
                        if plan.stop is not None:
                            if plan.exit_now:
                                ex, reason = cl - abs(cl) * slip, "lock_close"
                                break
                            X.apply_stop(st, plan.stop, plan.stop_source)
                            moves += 1
                    # 4) trailing (sadece kapanmış 4s mumlar)
                    k4 = int(np.searchsorted(close4, t_close, side="right")) - 1
                    if k4 >= 0:
                        n_sw = cfg.trail_swing_n
                        cands = X.trail_candidates(st, cfg, float(cl), float(a_arr[k4]),
                                                   c.low[max(0, k4 - n_sw + 1):k4 + 1], c.high[max(0, k4 - n_sw + 1):k4 + 1])
                        new, src = X.choose_stop(st, cands)
                        if new is not None:
                            if new >= cl:
                                ex, reason = cl - abs(cl) * slip, "trail_close"
                                break
                            X.apply_stop(st, new, src)
                            moves += 1
                # 5) funding (pozisyon açıkken geçen funding anları)
                if len(f_t):
                    a_, b_ = np.searchsorted(f_t, [int(sub.time[j]) + 1, t_close + 1])
                    for q in range(a_, b_):
                        fund_r += f_r[q] * abs(cl) * frac * d_real / st.one_r
                j += 1
            if ex is None:
                j = len(sub) - 1
                ex, reason = sub.close[j], "end"
            fills.append((frac, float(ex), reason))
            gross = sum(fr * (p - e) for fr, p, _ in fills)
            fees = (abs(e) + sum(fr * abs(p) for fr, p, _ in fills)) * fee
            r = (gross - fees) / st.one_r - fund_r
            last_reason = {"end": "Test sonu (açık)", "all_partial": "Kısmi TP'lerle tamamen kapandı",
                           "lock_close": "Kâr kilidi (kapanışta)", "trail_close": "İz süren stop (kapanışta)"}.get(reason)
            if last_reason is None:
                last_reason = ("Stop" if st.stop_source == "initial" else
                               "Kâr kilidi stopu" if st.stop_source.startswith("kâr") else "İz süren stop")
                if reason == "gap":
                    last_reason += " (mum stopun ötesinde açıldı)"
            exit_close = int(sub_close[j])
            out.append({
                "symbol": symbol, "side": side, "status": "closed", "score": None,
                "setup_time": int(close4[i]), "entry_time": int(sub.time[j0]),
                "exit_time": exit_close,
                "entry_price": px(e), "exit_price": px(float(ex)), "sl": px(st.stop), "sl_initial": px(stop), "tp1": None, "tp2": None,
                "tp1_hit": int(bool(st.partials_done)), "r_multiple": r, "fee_r": fees / st.one_r, "funding_r": fund_r,
                "exit_reason": last_reason, "mfe": mfe, "mae": mae, "best_r": st.best_r, "stop_moves": moves,
                "partials": [{"tier": nm, "frac": fr, "price": px(p)} for fr, p, nm in fills[:-1]],
                "reasons": [{"ok": True, "pts": 0, "text": f"4s kırılım: son {N} mum seviyesi {fmt_price(px(level))}"}],
                "annotations": {"tf": "4h", "strategy": "trend", "init_stop": px(stop),
                                "breakout": {"price": px(level), "time": int(c.time[i]), "from_time": int(c.time[i - N])}},
            })
            i = int(np.searchsorted(close4, exit_close, side="right"))
            i = max(i, N + 20)
    return out
