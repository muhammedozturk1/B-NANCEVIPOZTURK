"""Backtest için historical veri indirir (public endpoint, API key gerekmez).

Örnek:
    python -m scripts.smc.download_history --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 60 --out data
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from backend.smc.backtest.data import download_klines, save_csv, save_filters, save_funding
from backend.smc.market_data.rest import BinanceREST


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", default="data")
    ap.add_argument("--base-url", default="https://fapi.binance.com")
    a = ap.parse_args()
    out = Path(a.out)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    end = int(time.time() * 1000)
    start = end - a.days * 86_400_000
    rest = BinanceREST(a.base_url, weight_per_min=1200)
    try:
        info = await rest.exchange_info()
        out.mkdir(parents=True, exist_ok=True)
        save_filters(out / "exchange_info.json", info, symbols)
        for sym in symbols:
            for tf in ("3m", "5m", "15m"):
                rows = await download_klines(rest, sym, tf, start, end)
                save_csv(out / f"{sym}_{tf}.csv", rows)
                print(f"{sym} {tf}: {len(rows)} mum")
            save_funding(out / f"{sym}_funding.json", await rest.funding_rate_history(sym, start_time=start))
    finally:
        await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
