"""
STRATEJİ LABORATUVARI - bot yazmadan ÖNCE fikri uzun geçmişte sınar.

Şimdiye kadar denenenlerin hepsi "kısa vadede dönüş noktasını tahmin et, yakın hedef al"
türündendi ve hepsi zarar etti. Burada sınanan aile bunun TERSİDİR: TREND TAKİBİ.
  - Giriş : fiyat son N mumun en yükseğinin üstünde kapanırsa LONG (en düşüğünün altında SHORT)
  - Stop  : girişten 2 x ATR uzakta (geniş: gürültüyle vurulmaz, komisyon riske göre çok küçük kalır)
  - Çıkış : fiyat son M mumun en düşüğünün altında kapanırsa (trend bitti). Sabit hedef YOK:
            az sayıda büyük kazanç, çok sayıda küçük kayıp beklenir.

Kullanım (internet gerekir, API key gerekmez):
    python -u -m backend.lab --tf 1d --days 1095 --top 60
    python -u -m backend.lab --tf 4h --days 730 --top 40

Dürüstlük kuralları:
  - 6 varyant ÖNCEDEN sabit; sonuçlara bakıp parametre seçilmez.
  - Dönem ikiye bölünür: ilk 2/3 (A) ve son 1/3 (B). Bir varyant ancak HER İKİ dönemde de
    kârlıysa "GEÇTİ" sayılır. Tek dönemde kârlı olmak şans olabilir.
  - Sinyal mum kapanışında, giriş bir sonraki mumun açılışında. Komisyon + kayma + (long için)
    fonlama maliyeti düşülür.
  - Coinler BUGÜNÜN hacmine göre seçildiği için sonuçlar biraz iyimserdir (batan coinler listede yok).

Sonuçlar R cinsindendir: 1R = o işlemde stop olunca kaybedilecek tutar.
"""
import argparse
from collections import defaultdict

import numpy as np

from backend import config
from backend.backtest import load_ohlcv, pick_symbols, public_exchange
from backend.strategies import indicators as ind

DAY_MS = 86_400_000
FUNDING_PER_DAY = 0.0003       # long tutmanın yaklaşık günlük fonlama maliyeti (%0.03); short için 0 sayılır
ATR_MULT = 2.0
ATR_LEN = 20
REGIME_DAYS = 100              # BTC bu kadar günlük ortalamasının üstündeyse "boğa" rejimi

# (ad, giriş kanalı, çıkış kanalı, short var mı, BTC rejim filtresi var mı)
VARIANTS = [
    ("T1  20/10 sadece long",        20, 10, False, False),
    ("T2  55/20 sadece long",        55, 20, False, False),
    ("T3  20/10 long+short",         20, 10, True,  False),
    ("T4  55/20 long+short",         55, 20, True,  False),
    ("T5  20/10 long+short +rejim",  20, 10, True,  True),
    ("T6  55/20 long+short +rejim",  55, 20, True,  True),
]


def _prior_max(x: np.ndarray, n: int) -> np.ndarray:
    """out[i] = max(x[i-n:i])  (i. mum HARİÇ önceki n mum). İlk n eleman NaN."""
    out = np.full(len(x), np.nan)
    if len(x) > n:
        out[n:] = np.lib.stride_tricks.sliding_window_view(x, n)[:-1].max(axis=1)
    return out


def _prior_min(x: np.ndarray, n: int) -> np.ndarray:
    return -_prior_max(-x, n)


def btc_regime(a: dict, tf_ms: int) -> dict:
    """{mum_ts: +1 boğa / -1 ayı} - o mumun kapanışı, kendi ortalamasına göre."""
    n = max(10, int(REGIME_DAYS * DAY_MS / tf_ms))
    c = a["c"]
    if len(c) <= n:
        return {}
    sma = np.convolve(c, np.ones(n) / n, mode="valid")          # sma[k] = ortalama(c[k:k+n])
    out = {}
    for i in range(n - 1, len(c)):
        out[int(a["ts"][i])] = 1 if c[i] > sma[i - n + 1] else -1
    return out


def trend_trades(symbol: str, a: dict, n_in: int, n_out: int, allow_short: bool,
                 regime: dict, tf_ms: int) -> list:
    o, h, l, c, ts = a["o"], a["h"], a["l"], a["c"], a["ts"]
    n = len(c)
    if n < n_in + ATR_LEN + 5:
        return []
    atr = ind.atr(h, l, c, ATR_LEN)
    hi_in, lo_in = _prior_max(h, n_in), _prior_min(l, n_in)
    hi_out, lo_out = _prior_max(h, n_out), _prior_min(l, n_out)
    cost = config.COST_PER_SIDE

    trades = []
    i = max(n_in, ATR_LEN + 1)
    while i < n - 1:
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        d = 0
        if c[i] > hi_in[i]:
            d = 1
        elif allow_short and c[i] < lo_in[i]:
            d = -1
        if d != 0 and regime is not None and regime.get(int(ts[i]), 0) != d:
            d = 0
        if d == 0:
            i += 1
            continue

        e = i + 1
        entry = o[e]
        stop = entry - d * ATR_MULT * atr[i]
        risk_pct = abs(entry - stop) / entry
        if risk_pct <= 0 or (d > 0 and stop <= 0):
            i += 1
            continue

        exit_px, j_exit = None, None
        for j in range(e, n):
            if (l[j] <= stop) if d > 0 else (h[j] >= stop):
                exit_px = (min(stop, o[j]) if d > 0 else max(stop, o[j]))   # boşlukla açıldıysa daha kötü fiyat
                j_exit = j
                break
            trend_over = (c[j] < lo_out[j]) if d > 0 else (c[j] > hi_out[j])
            if trend_over and j + 1 < n:
                exit_px, j_exit = o[j + 1], j + 1
                break
        if exit_px is None:
            break                                   # veri bitti, işlem hâlâ açık: sayma

        days = (int(ts[j_exit]) - int(ts[e])) / DAY_MS
        gross = d * (exit_px - entry) / entry
        net = gross - 2 * cost - (FUNDING_PER_DAY * days if d > 0 else 0.0)
        trades.append({"symbol": symbol, "d": d, "entry_ts": int(ts[e]), "exit_ts": int(ts[j_exit]),
                       "r": float(net / risk_pct), "risk_pct": float(risk_pct), "days": float(days)})
        i = j_exit                                  # çıkış mumundan itibaren yeni sinyal aranabilir
    return trades


def r_stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "avg": 0.0, "pf": 0.0, "total": 0.0, "win": 0.0}
    r = np.array([t["r"] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    return {"n": len(r), "win": len(wins) / len(r), "avg": float(r.mean()), "total": float(r.sum()),
            "pf": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "days": float(np.mean([t["days"] for t in trades]))}


def portfolio(trades: list, start: float, risk_frac: float, max_pos: int, max_lev: float) -> dict:
    """Kasa simülasyonu: her işlemde kasanın risk_frac kadarı riske edilir (bileşik)."""
    equity, peak, max_dd = start, start, 0.0
    open_tr, taken = [], 0

    def settle(until):
        nonlocal equity, peak, max_dd
        for tr in sorted([x for x in open_tr if x["exit_ts"] <= until], key=lambda x: x["exit_ts"]):
            open_tr.remove(tr)
            equity += tr["r"] * tr["risk_usd"]
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)

    for tr in sorted(trades, key=lambda x: x["entry_ts"]):
        settle(tr["entry_ts"])
        if equity <= 0:
            break
        if len(open_tr) >= max_pos or any(x["symbol"] == tr["symbol"] for x in open_tr):
            continue
        risk_usd = equity * risk_frac
        notional = risk_usd / tr["risk_pct"]
        if sum(x["notional"] for x in open_tr) + notional > equity * max_lev:
            continue
        open_tr.append(dict(tr, risk_usd=risk_usd, notional=notional))
        taken += 1
    settle(float("inf"))
    return {"final": equity, "max_dd": max_dd, "taken": taken}


def _fmt(s: dict) -> str:
    if not s["n"]:
        return "işlem yok"
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"işlem {s['n']:>4} | kazanan %{s['win'] * 100:>3.0f} | ort.kazanç {s['avg_win']:+.2f}R | "
            f"ort.kayıp {s['avg_loss']:+.2f}R | işlem başı {s['avg']:+.3f}R | PF {pf:>5} | toplam {s['total']:+7.1f}R "
            f"| ort.süre {s['days']:.0f} gün")


def passed(sa: dict, sb: dict) -> bool:
    return all(s["n"] >= 40 and s["avg"] > 0.05 and s["pf"] > 1.15 for s in (sa, sb))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d", choices=["4h", "1d"])
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--risk", type=float, default=0.02, help="işlem başı kasa riski (0.02 = %2)")
    ap.add_argument("--max-pos", type=int, default=5)
    ap.add_argument("--max-lev", type=float, default=3.0, help="açık pozisyonların toplamı / kasa tavanı")
    ap.add_argument("--balance", type=float, default=config.STARTING_BALANCE)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    tf_ms = ind.TF_MS[args.tf]
    ex = None if args.offline else public_exchange()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or \
        (pick_symbols(ex, args.top) if ex else config.FALLBACK_SYMBOLS)
    if "BTC/USDT" not in symbols:
        symbols = ["BTC/USDT"] + symbols

    warm_days = int(120 * max(1, DAY_MS / tf_ms) * tf_ms / DAY_MS) + 30
    print(f"\n=== TREND TAKİBİ | {args.tf} | {args.days} gün | {len(symbols)} parite ===")
    data = {}
    for sym in symbols:
        try:
            a = ind.to_arrays(load_ohlcv(ex, sym, args.tf, args.days + warm_days, args.offline))
            if len(a["c"]) >= 120:
                data[sym] = a
            print(f"  {sym:<14} {len(a['c']):>5} mum")
        except Exception as e:
            print(f"  {sym}: HATA {e}")
    if "BTC/USDT" not in data:
        print("BTC verisi yok, çıkılıyor.")
        return

    regime = btc_regime(data["BTC/USDT"], tf_ms)
    btc = data["BTC/USDT"]
    t_end = int(btc["ts"][-1])
    t_start = t_end - args.days * DAY_MS
    t_split = t_start + int(args.days * 2 / 3) * DAY_MS

    def btc_ret(t0, t1):
        m = (btc["ts"] >= t0) & (btc["ts"] <= t1)
        return btc["c"][m][-1] / btc["c"][m][0] - 1 if m.sum() > 1 else 0.0

    print(f"\nDönem A (ilk 2/3): BTC al-tut {btc_ret(t_start, t_split) * 100:+.0f}%   |   "
          f"Dönem B (son 1/3): BTC al-tut {btc_ret(t_split, t_end) * 100:+.0f}%")
    print(f"Kasa simülasyonu: {args.balance:.0f}$ başlangıç, işlem başı risk %{args.risk * 100:.0f}, "
          f"en fazla {args.max_pos} pozisyon, toplam kaldıraç tavanı {args.max_lev:.0f}x\n")

    any_pass = False
    for name, n_in, n_out, short, use_regime in VARIANTS:
        trades = []
        for sym, a in data.items():
            trades += trend_trades(sym, a, n_in, n_out, short, regime if use_regime else None, tf_ms)
        trades = [t for t in trades if t["entry_ts"] >= t_start]
        ta = [t for t in trades if t["entry_ts"] < t_split]
        tb = [t for t in trades if t["entry_ts"] >= t_split]
        sa, sb = r_stats(ta), r_stats(tb)
        ok = passed(sa, sb)
        any_pass |= ok
        pf_all = portfolio(trades, args.balance, args.risk, args.max_pos, args.max_lev)
        pf_b = portfolio(tb, args.balance, args.risk, args.max_pos, args.max_lev)
        print(f"{'✅ GEÇTİ ' if ok else '❌ kaldı '} {name}")
        print(f"     A: {_fmt(sa)}")
        print(f"     B: {_fmt(sb)}")
        longs, shorts = [t for t in trades if t["d"] > 0], [t for t in trades if t["d"] < 0]
        if shorts:
            print(f"     long: {r_stats(longs)['avg']:+.3f}R ({len(longs)})  |  short: {r_stats(shorts)['avg']:+.3f}R ({len(shorts)})")
        print(f"     kasa (tüm dönem): {args.balance:.0f}$ -> {pf_all['final']:.0f}$ | en büyük düşüş %{pf_all['max_dd'] * 100:.0f} "
              f"| açılan işlem {pf_all['taken']}   ||   sadece B dönemi: {args.balance:.0f}$ -> {pf_b['final']:.0f}$ "
              f"(düşüş %{pf_b['max_dd'] * 100:.0f})\n")

    print("SONUÇ:")
    if any_pass:
        print("  En az bir varyant iki dönemde de kârlı. Bu, bot yazmaya değer bir başlangıç noktası.")
        print("  Not: en büyük düşüş yüzdesine bak - o düşüşü yaşamaya hazır değilsen risk oranını düşür.")
    else:
        print("  Hiçbir varyant iki dönemde birden kârlı değil. Bu aile için de bot yazmaya değmez.")


if __name__ == "__main__":
    main()
