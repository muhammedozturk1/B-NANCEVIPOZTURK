"""Historical kline verisi indirme / okuma (Binance public REST, kimlik doğrulama gerekmez)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from backend.smc.core.types import TIMEFRAME_MS, Candle
from backend.smc.market_data.rest import BinanceREST
from backend.smc.market_data.universe import SymbolFilters, parse_symbol_filters


async def download_klines(rest: BinanceREST, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    rows: list[list] = []
    cur = start_ms
    step = TIMEFRAME_MS[interval]
    while cur < end_ms:
        batch = await rest.klines(symbol, interval, limit=1000, start_time=cur, end_time=end_ms)
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1][0]) + step
        if len(batch) < 1000:
            break
    seen, out = set(), []
    for r in rows:
        if r[0] not in seen and int(r[6]) < end_ms:
            seen.add(r[0])
            out.append(r)
    return out


def save_csv(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerows(rows)


def load_csv(path: Path) -> list[Candle]:
    with path.open() as fh:
        return [Candle.from_rest(r) for r in csv.reader(fh) if r]


def load_dataset(root: Path, symbols: list[str], tfs: tuple[str, ...] = ("3m", "5m", "15m")) -> dict[str, dict[str, list[Candle]]]:
    data = {}
    for sym in symbols:
        data[sym] = {tf: load_csv(root / f"{sym}_{tf}.csv") for tf in tfs}
    return data


def save_filters(path: Path, exchange_info: dict, symbols: list[str]) -> None:
    out = {s["symbol"]: s for s in exchange_info.get("symbols", []) if s["symbol"] in symbols}
    path.write_text(json.dumps(out))


def load_filters(path: Path) -> dict[str, SymbolFilters]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: f for k, v in raw.items() if (f := parse_symbol_filters(v)) is not None}


def save_funding(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps([(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]))


def load_funding(path: Path) -> list[tuple[int, float]]:
    return [tuple(x) for x in json.loads(path.read_text())] if path.exists() else []
