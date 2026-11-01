"""
FONLAMA STRATEJİSİ BACKTEST - gerçek Binance geçmiş fonlama verisiyle.

Kullanım (Codespace'te):
    python -u -m backend.funding.backtest --days 1095

Canlı motorla AYNI kurallar (backend/funding/strategy.py) kullanılır.
Her 8 saatte bir karar verilir; girişte/çıkışta iki bacağın komisyon+kayması düşülür.

Varsayımlar (dürüstçe):
  - Spot ve vadeli fiyat farkındaki (baz) küçük değişimler yok sayılır; gerçekte
    bunlar işlem başına genelde %0.1-0.3 civarı ek kazanç/kayıp yaratır.
  - Bugünün likit coinleri seçilir (geçmişte batmış coinler listede yok -> hafif iyimser).
"""
import argparse
import os
import time
from datetime import datetime, timezone

import pandas as pd

from backend import config
from backend.funding import strategy as fs

DATA_DIR = "data"
STEP_MS = 8 * fs.HOUR_MS


def simulate(data: dict, start_ms: int, end_ms: int) -> dict:
    """data: {sym: {"events": [(ts, rate)], "closes": [(ts, close)]}} -> sonuçlar.
    Getiriler NOMİNALİN ORANI olarak hesaplanır; kasa getirisi = toplam * position_pct."""
    import numpy as np
    c = config.FUNDING
    cost = fs.round_trip_cost()
    series = {s: fs.Series(d["events"]) for s, d in data.items()}
    closes = {s: (np.array([t for t, _ in d["closes"]], dtype=np.int64),
                  np.array([p for _, p in d["closes"]], dtype=float)) for s, d in data.items()}
    open_pos, trades = {}, []

    def price_at(sym, t):
        ts, px = closes[sym]
        i = int(np.searchsorted(ts + 86_400_000, t, "right"))  # t anında KAPANMIŞ son günlük mum
        return float(px[i - 1]) if i > 0 else None

    t = (start_ms // STEP_MS + 1) * STEP_MS
    while t <= end_ms:
        # 1) tahsilat
        for sym, p in open_pos.items():
            total, n = series[sym].sum_between(p["last"], t)
            p["funding"] += total
            p["n"] += n
            p["last"] = t
        # 2) çıkış
        for sym in list(open_pos):
            p = open_pos[sym]
            stats = fs.series_stats(series[sym], t)
            px = price_at(sym, t)
            move = (px / p["px"] - 1) if (px and p["px"]) else 0.0
            reason = fs.exit_reason(stats, move, (t - p["open"]) / fs.HOUR_MS)
            if reason:
                trades.append({"symbol": sym, "open": p["open"], "close": t, "reason": reason,
                               "funding": p["funding"], "payments": p["n"], "net": p["funding"] - cost})
                del open_pos[sym]
        # 3) giriş
        if len(open_pos) < c["max_positions"]:
            scored = []
            for sym in data:
                if sym in open_pos:
                    continue
                stats = fs.series_stats(series[sym], t)
                if fs.entry_ok(stats):
                    scored.append((stats["avg_apr"], sym))
            for _, sym in sorted(scored, reverse=True)[: c["max_positions"] - len(open_pos)]:
                open_pos[sym] = {"open": t, "last": t, "funding": 0.0, "n": 0, "px": price_at(sym, t)}
        t += STEP_MS

    for sym, p in open_pos.items():  # dönem sonunda açık kalanlar kapatılmış sayılır
        trades.append({"symbol": sym, "open": p["open"], "close": end_ms, "reason": "donem_sonu",
                       "funding": p["funding"], "payments": p["n"], "net": p["funding"] - cost})
    return {"trades": trades, "start": start_ms, "end": end_ms}


def report(res: dict) -> dict:
    c = config.FUNDING
    tr = pd.DataFrame(res["trades"])
    years = (res["end"] - res["start"]) / (365 * 86_400_000)
    if tr.empty:
        return {"trades": 0}
    tr["days"] = (tr["close"] - tr["open"]) / 86_400_000
    tr["year"] = pd.to_datetime(tr["close"], unit="ms").dt.year
    # Kasa getirisi: her pozisyon kasanın position_pct'i kadar nominal, bunun 1.5 katı sermaye bağlar
    total_return = tr["net"].sum() * c["position_pct"]
    by_year = {}
    for y, g in tr.groupby("year"):
        by_year[int(y)] = {"islem": int(len(g)), "kasa_getirisi_%": round(float(g["net"].sum() * c["position_pct"] * 100), 2)}
    return {
        "islem_sayisi": int(len(tr)),
        "kazanan_oran": round(float((tr["net"] > 0).mean()), 3),
        "ort_tutma_gun": round(float(tr["days"].mean()), 1),
        "ort_islem_net_%": round(float(tr["net"].mean() * 100), 3),
        "toplam_kasa_getirisi_%": round(float(total_return * 100), 2),
        "yillik_kasa_getirisi_%": round(float(total_return / years * 100), 2),
        "en_kotu_islem_%": round(float(tr["net"].min() * 100), 3),
        "yila_gore": by_year,
        "en_cok_kazandiran": tr.groupby("symbol")["net"].sum().sort_values(ascending=False).head(5).round(4).to_dict(),
    }


def load_data(market, symbols: list, days: int) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    since = int(time.time() * 1000) - days * 86_400_000
    data = {}
    for sym in symbols:
        name = sym.replace("/", "")
        fpath = os.path.join(DATA_DIR, f"funding_{name}.csv")
        try:
            if os.path.exists(fpath):
                ev = pd.read_csv(fpath)
            else:
                ev = pd.DataFrame([(t, r) for t, r, _ in market.history(sym, since)], columns=["ts", "rate"])
                ev.to_csv(fpath, index=False)
            ev = ev[ev["ts"] >= since]
            kl = market.fut.fetch_ohlcv(market.perp(sym), "1d", since=since, limit=1500)
            if len(ev) < 30 or not kl:
                print(f"  {sym}: yetersiz veri, atlandı")
                continue
            data[sym] = {"events": list(zip(ev["ts"].astype("int64"), ev["rate"].astype(float))),
                         "closes": [(int(k[0]), float(k[4])) for k in kl]}
            print(f"  {sym}: {len(ev)} fonlama kaydı")
        except Exception as e:
            print(f"  {sym}: HATA {e}")
    return data


def compare_thresholds(data: dict, start_ms: int, end_ms: int, thresholds) -> list:
    """Aynı veride farklı giriş eşiklerini dener. Diğer kurallar aynı kalır."""
    c = config.FUNDING
    saved = (c["entry_min_apr"], c["entry_last_min_apr"])
    rows = []
    try:
        for apr in thresholds:
            c["entry_min_apr"] = apr
            c["entry_last_min_apr"] = min(saved[1], apr)
            rep = report(simulate(data, start_ms, end_ms))
            rows.append((apr, rep))
    finally:
        c["entry_min_apr"], c["entry_last_min_apr"] = saved
    return rows


def print_comparison(rows, start_ms: int, end_ms: int):
    c = config.FUNDING
    print(f"\n=== EŞİK KARŞILAŞTIRMASI | kasa {config.STARTING_BALANCE:.0f}$ | "
          f"en fazla {c['max_positions']} pozisyon x kasanın %{c['position_pct'] * 100:.0f}'i ===")
    print(f"{'Eşik':>6} | {'İşlem':>5} | {'Kârlı':>6} | {'Ort.gün':>7} | {'Toplam kasa':>11} | {'Yıllık':>7} | {'En kötü işlem':>13}")
    print("-" * 78)
    for apr, r in rows:
        if not r.get("islem_sayisi"):
            print(f"%{apr * 100:>5.0f} | {0:>5} | {'-':>6} | {'-':>7} | {'-':>11} | {'-':>7} | {'-':>13}")
            continue
        print(f"%{apr * 100:>5.0f} | {r['islem_sayisi']:>5} | %{r['kazanan_oran'] * 100:>5.0f} | "
              f"{r['ort_tutma_gun']:>7} | %{r['toplam_kasa_getirisi_%']:>10} | %{r['yillik_kasa_getirisi_%']:>6} | "
              f"%{r['en_kotu_islem_%']:>12}")
    print("\nOkuma: 'Toplam kasa' ve 'Yıllık' kasanın tamamına göre net getiri (komisyon dahil).")
    print("'En kötü işlem' tek bir pozisyonun kendi büyüklüğüne göre net sonucu.")
    print("Yıllara göre (her eşik):")
    for apr, r in rows:
        if r.get("yila_gore"):
            print(f"  %{apr * 100:.0f}: " + ", ".join(f"{y}: %{v['kasa_getirisi_%']} ({v['islem']} işlem)"
                                                   for y, v in r["yila_gore"].items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--top", type=int, default=config.TOP_SYMBOLS_COUNT)
    ap.add_argument("--compare", action="store_true",
                    help="%15, %12, %10 ve %8 giriş eşiklerini aynı veride karşılaştır")
    args = ap.parse_args()

    from backend.funding.market import FundingMarket
    market = FundingMarket()
    symbols = market.candidates(args.top)
    print(f"\n=== FONLAMA BACKTEST | {args.days} gün | {len(symbols)} parite (spot+vadeli) ===")
    print("Veri indiriliyor, bu birkaç dakika sürebilir...")
    data = load_data(market, symbols, args.days)
    end = int(time.time() * 1000)
    start = end - args.days * 86_400_000

    if args.compare:
        rows = compare_thresholds(data, start, end, [0.15, 0.12, 0.10, 0.08])
        print_comparison(rows, start, end)
        return

    res = simulate(data, start, end)
    rep = report(res)
    c = config.FUNDING
    print(f"\nKurallar: giriş >= yıllık %{c['entry_min_apr']*100:.0f}, çıkış < %{c['exit_apr']*100:.0f}, "
          f"en fazla {c['max_positions']} pozisyon, pozisyon başı kasanın %{c['position_pct']*100:.0f}'i, "
          f"gidiş-dönüş maliyet %{fs.round_trip_cost()*100:.2f}")
    print("\n--- SONUÇ ---")
    for k, v in rep.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
