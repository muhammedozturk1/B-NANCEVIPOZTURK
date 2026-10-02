"""
AI MODELİ EĞİTİMİ + BACKTEST

Kullanım (kendi bilgisayarında, internet gerekir - API key GEREKMEZ):
    python -m backend.ai.train --engine scalp --days 365
    python -m backend.ai.train --engine day --days 730

Ne yapar:
  1) Binance Futures (gerçek piyasa) geçmiş mumlarını indirir -> data/ klasörüne önbellekler
  2) Kurulumların (setups) tetiklendiği her mumu bulur, işlemi komisyon dahil simüle eder
  3) Zamana göre böler: %60 eğitim / %20 doğrulama (eşik seçimi) / %20 TEST (hiç görülmemiş)
  4) Gradient Boosting sınıflandırıcı eğitir: "bu işlem komisyon sonrası kâr eder mi?"
  5) TEST döneminde: AI'sız (tüm sinyaller) vs AI filtreli sonuçları raporlar
  6) Test başarılıysa modeli "onaylı" olarak models/<engine>.joblib'e kaydeder.
     Onaysızsa bot (AI_MODE=required iken) o motorla işlem AÇMAZ.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from backend import config
from backend.strategies import features as feat
from backend.strategies.indicators import TF_MS
from backend.ai import dataset as ds

DATA_DIR = "data"


# ----------------------------------------------------------------------
# VERİ
# ----------------------------------------------------------------------
def _public_exchange():
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    ex.load_markets()
    return ex


def top_symbols(ex, count: int) -> list:
    """Canlı botla AYNI tarama kuralları (backend/exchange/universe.py)."""
    from backend.exchange import universe
    return universe.select_symbols(ex, ex.fetch_tickers(), count, config.MIN_24H_VOLUME_USDT)


def load_ohlcv(ex, symbol: str, tf: str, days: int, offline: bool) -> list:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol.replace('/', '')}_{tf}.csv")
    since = int(time.time() * 1000) - days * 86_400_000
    cached = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(
        columns=["ts", "open", "high", "low", "close", "volume"])

    if not offline:
        if len(cached) and cached["ts"].min() <= since + TF_MS[tf]:
            start = int(cached["ts"].max()) + TF_MS[tf]   # sadece eksik kısmı indir
        else:
            cached, start = cached.iloc[0:0], since
        rows = []
        while True:
            batch = ex.fetch_ohlcv(symbol, tf, since=start, limit=1500)
            if not batch:
                break
            rows.extend(batch)
            start = batch[-1][0] + TF_MS[tf]
            if len(batch) < 1500 or start > time.time() * 1000:
                break
        if rows:
            new = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            cached = new if cached.empty else pd.concat([cached, new])
            cached = cached.drop_duplicates("ts").sort_values("ts")
            # son (kapanmamış) mumu kaydetme
            cached = cached[cached["ts"] + TF_MS[tf] <= time.time() * 1000]
            cached.to_csv(path, index=False)

    cached = cached[cached["ts"] >= since]
    return cached[["ts", "open", "high", "low", "close", "volume"]].values.tolist()


# ----------------------------------------------------------------------
# EĞİTİM
# ----------------------------------------------------------------------
def time_split(df: pd.DataFrame, embargo_ms: int):
    t1, t2 = df["ts"].quantile(0.6), df["ts"].quantile(0.8)
    # Embargo: bölünme sınırına yakın örneklerin sonucu bir sonraki dönemle
    # çakışır (sızıntı). Sınırdan önceki max_hold süresindeki örnekleri at.
    train = df[df["exit_ts"] < t1 - embargo_ms]
    val = df[(df["ts"] >= t1) & (df["exit_ts"] < t2 - embargo_ms)]
    test = df[df["ts"] >= t2]
    return train, val, test


def pick_threshold(val: pd.DataFrame, probs: np.ndarray, min_trades: int):
    best_t, best_total = None, -np.inf
    v = val.copy()
    v["p"] = probs
    for t in np.arange(0.40, 0.81, 0.01):
        sel = ds.sequential_filter(v[v["p"] >= t])
        if len(sel) < min_trades:
            continue
        total = sel["net_r"].sum()
        if sel["net_r"].mean() > 0 and total > best_total:
            best_t, best_total = round(float(t), 2), total
    return best_t


def select_setups(val: pd.DataFrame, p_val: np.ndarray, min_trades: int = 15) -> list:
    """Kurulum seçimi DOĞRULAMA döneminde yapılır (test dönemine bakılmaz).
    Her kurulum için ayrı ayrı: AI filtresiyle kârlı bir eşik bulunabiliyor mu?"""
    keep = []
    for name in sorted(val["setup"].unique()):
        mask = (val["setup"] == name).values
        if mask.sum() < min_trades:
            continue
        if pick_threshold(val[mask], p_val[mask], min_trades) is not None:
            keep.append(name)
    return keep


def make_model():
    return HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.05, max_iter=400, min_samples_leaf=80,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
        random_state=42,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="scalp", choices=list(config.ENGINE_TIMEFRAMES))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--symbols", default="", help="virgüllü liste, boşsa en hacimli N parite")
    ap.add_argument("--top", type=int, default=config.TOP_SYMBOLS_COUNT)
    ap.add_argument("--offline", action="store_true", help="sadece data/ klasöründeki CSV'leri kullan")
    args = ap.parse_args()

    engine = args.engine
    tfs = config.ENGINE_TIMEFRAMES[engine]
    ex = None if args.offline else _public_exchange()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or \
        (top_symbols(ex, args.top) if ex else config.FALLBACK_SYMBOLS)

    print(f"\n=== {engine.upper()} | {tfs['entry']}/{tfs['confirm']} | {args.days} gün | {len(symbols)} parite ===")
    print("Pariteler: " + ", ".join(s.split("/")[0] for s in symbols))
    parts = []
    for sym in symbols:
        try:
            entry = load_ohlcv(ex, sym, tfs["entry"], args.days, args.offline)
            confirm = load_ohlcv(ex, sym, tfs["confirm"], args.days + 30, args.offline)
            if len(entry) < 1000:
                print(f"  {sym}: yetersiz veri ({len(entry)} mum), atlandı")
                continue
            part = ds.build_symbol_dataset(sym, entry, confirm, engine)
            print(f"  {sym}: {len(entry)} mum -> {len(part)} aday sinyal")
            parts.append(part)
        except Exception as e:
            print(f"  {sym}: HATA {e}")
    if not parts:
        print("Veri yok, çıkılıyor.")
        return
    df = pd.concat(parts).sort_values("ts").reset_index(drop=True)

    embargo = config.EXIT_PARAMS[engine]["max_hold_bars"] * TF_MS[tfs["entry"]]
    train, val, test = time_split(df, embargo)
    print(f"\nÖrnek sayısı -> eğitim: {len(train)}, doğrulama: {len(val)}, test: {len(test)}")
    if len(train) < 500 or len(val) < 100 or len(test) < 100:
        print("⚠️ Güvenilir bir model için veri çok az. --days artır veya --top artır.")
        return

    X_cols = feat.MODEL_FEATURES
    model = make_model()
    model.fit(train[X_cols], (train["net_r"] > 0).astype(int))

    p_val = model.predict_proba(val[X_cols])[:, 1]
    p_test = model.predict_proba(test[X_cols])[:, 1]
    auc_val = roc_auc_score((val["net_r"] > 0).astype(int), p_val)
    auc_test = roc_auc_score((test["net_r"] > 0).astype(int), p_test)

    appr = config.AI_APPROVAL
    setups_kept = select_setups(val, p_val)
    print(f"\nDoğrulama döneminde kârlı bulunan kurulumlar: {setups_kept or 'YOK'}")

    base_test = ds.performance(ds.sequential_filter(test))
    threshold, ai_test, by_setup = None, {"trades": 0}, {}
    if setups_kept:
        vmask = val["setup"].isin(setups_kept).values
        threshold = pick_threshold(val[vmask], p_val[vmask], min_trades=max(20, appr["min_test_trades"] // 2))
    if threshold is not None:
        t = test.copy()
        t["p"] = p_test
        t = t[t["setup"].isin(setups_kept)]
        ai_sel = ds.sequential_filter(t[t["p"] >= threshold])
        ai_test = ds.performance(ai_sel)
        by_setup = {k: ds.performance(g) for k, g in ai_sel.groupby("setup")}

    approved = (
        threshold is not None
        and ai_test.get("trades", 0) >= appr["min_test_trades"]
        and ai_test.get("avg_r", -1) >= appr["min_avg_r"]
        and (ai_test.get("profit_factor") or 0) >= appr["min_profit_factor"]
    )

    report = {
        "engine": engine, "timeframes": tfs, "days": args.days, "symbols": symbols,
        "exit_params": config.EXIT_PARAMS[engine], "cost_per_side": config.COST_PER_SIDE,
        "auc_validation": round(float(auc_val), 3), "auc_test": round(float(auc_test), 3),
        "threshold": threshold, "setups": setups_kept,
        "test_without_ai": base_test, "test_with_ai": ai_test, "test_with_ai_by_setup": by_setup,
        "approved": bool(approved),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }

    print("\n--- TEST DÖNEMİ (model bu veriyi hiç görmedi) ---")
    print(f"AUC (0.5 = yazı-tura): doğrulama {auc_val:.3f} | test {auc_test:.3f}")
    print(f"AI'sız (tüm sinyaller): {base_test}")
    print(f"AI filtreli (kurulumlar {setups_kept}, eşik {threshold}): {ai_test}")
    for k, v in by_setup.items():
        print(f"   - {k}: {v}")

    os.makedirs(config.AI_MODEL_DIR, exist_ok=True)
    with open(os.path.join(config.AI_MODEL_DIR, f"{engine}_report.json"), "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    joblib.dump({"model": model, "features": X_cols, "threshold": threshold,
                 "setups": setups_kept, "approved": bool(approved), "report": report},
                os.path.join(config.AI_MODEL_DIR, f"{engine}.joblib"))

    if approved:
        print(f"\n✅ Model ONAYLANDI -> {config.AI_MODEL_DIR}/{engine}.joblib")
    else:
        print(f"\n❌ Model onaylanmadı (test performansı yetersiz). Bot bu motorla işlem AÇMAYACAK.")
        print("   Bu kötü bir haber değil: para kaybetmeden önce öğrendin. Parametreleri/kurulumları değiştirip tekrar dene.")


if __name__ == "__main__":
    main()
