"""Training / Validation / Out-of-Sample backtest.

Parametreler config'den gelir ve segmentler arasında DEĞİŞTİRİLMEZ (aşırı optimizasyon koruması).
Parametre değişikliği yalnızca training sonuçlarına bakılarak yapılmalı; validation ile teyit edilip
out-of-sample bir kez, en sonda çalıştırılmalıdır.

    python -m scripts.smc.run_backtest --data data --symbols BTCUSDT,ETHUSDT,SOLUSDT --out reports
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from backend.smc.backtest.data import load_dataset, load_filters, load_funding
from backend.smc.backtest.engine import Backtester, split_periods
from backend.smc.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--segments", default="training,validation,out_of_sample")
    a = ap.parse_args()
    s = get_settings()
    root, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    symbols = [x.strip().upper() for x in a.symbols.split(",")]
    data = load_dataset(root, symbols)
    filters = load_filters(root / "exchange_info.json")
    funding = {sym: load_funding(root / f"{sym}_funding.json") for sym in symbols}
    all_t = [c.close_time for tfs in data.values() for c in tfs["3m"]]
    # İlk 1 gün indikatör ısınması için ayrılır
    start, end = min(all_t) + 86_400_000, max(all_t)
    report = {}
    for name, (seg_start, seg_end) in split_periods(start, end).items():
        if name not in a.segments.split(","):
            continue
        bt = Backtester(s, data, dict(filters), trade_start=seg_start, trade_end=seg_end, funding_rates=funding)
        res = bt.run()
        report[name] = {"period": [seg_start, seg_end], "signals": res.signals, **res.metrics}
        with (out / f"trades_{name}.csv").open("w", newline="") as fh:
            rows = [t.as_dict() for t in res.trades]
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        print(f"\n== {name} ==")
        for k, v in res.metrics.items():
            print(f"  {k:28s} {v:.4f}" if isinstance(v, float) else f"  {k:28s} {v}")
    (out / "backtest_report.json").write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
