"""
İşlem simülatörü - backtest ve AI etiketleme için.

Canlı botla BİREBİR aynı çıkış kuralları:
  - Giriş: sinyal mumu kapandıktan sonraki mumun açılışı
  - Stop: max(ATR * çarpan, fiyat * min_stop_pct)
  - Hedef: stop mesafesi * tp_r
  - Aynı mumda hem stop hem hedef değdiyse -> STOP sayılır (kötümser varsayım)
  - max_hold_bars dolunca kapanış fiyatından çıkış (zaman stopu)
  - Opsiyonel breakeven: fiyat breakeven_r'ye ulaşınca stop girişe (+maliyet) çekilir
Sonuç, komisyon + kayma DÜŞÜLMÜŞ R cinsinden döner.
"""
import numpy as np


def simulate(o, h, l, c, atr, idx, sides, params: dict, cost_per_side: float):
    n = len(c)
    k = len(idx)
    net_r = np.full(k, np.nan)
    bars = np.zeros(k, dtype=int)
    reason = np.array([""] * k, dtype=object)

    mult = params["sl_atr_mult"]
    min_pct = params["min_stop_pct"]
    tp_r = params["tp_r"]
    max_hold = params["max_hold_bars"]
    be_r = params.get("breakeven_r")

    for t in range(k):
        i = int(idx[t])
        side = int(sides[t])
        e = i + 1
        if e >= n or not np.isfinite(atr[i]):
            continue
        entry = o[e]
        dist = max(atr[i] * mult, entry * min_pct)
        sl = entry - side * dist
        tp = entry + side * dist * tp_r
        be_done = False
        exit_px, why, held = None, "", 0
        last = min(e + max_hold - 1, n - 1)
        for j in range(e, last + 1):
            held = j - e + 1
            if side > 0:
                if l[j] <= sl:
                    exit_px, why = min(sl, o[j]), ("be" if be_done else "sl")
                    break
                if h[j] >= tp:
                    exit_px, why = max(tp, o[j]) if o[j] > tp else tp, "tp"
                    break
                if be_r and not be_done and h[j] >= entry + be_r * dist:
                    sl, be_done = entry * (1 + 2 * cost_per_side), True
            else:
                if h[j] >= sl:
                    exit_px, why = max(sl, o[j]), ("be" if be_done else "sl")
                    break
                if l[j] <= tp:
                    exit_px, why = min(tp, o[j]) if o[j] < tp else tp, "tp"
                    break
                if be_r and not be_done and l[j] <= entry - be_r * dist:
                    sl, be_done = entry * (1 - 2 * cost_per_side), True
        if exit_px is None:
            if last - e + 1 < max_hold:
                continue  # veri bitti, sonuç bilinmiyor -> atla
            exit_px, why = c[last], "time"
        gross = side * (exit_px - entry) / dist
        cost = (entry + exit_px) * cost_per_side / dist
        net_r[t] = gross - cost
        bars[t] = held
        reason[t] = why
    return net_r, bars, reason
