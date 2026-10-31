"""Market universe: aktif USDT perpetual, yeterli hacim/derinlik/spread, geçerli symbol filtreleri."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from backend.smc.config import Settings
from backend.smc.market_data.rest import BinanceREST

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_max_qty: Decimal
    min_notional: float
    max_leverage: int = 10
    maint_margin_rate: float = 0.004
    maint_amount: float = 0.0

    def with_bracket(self, max_leverage: int, mmr: float, cum: float) -> "SymbolFilters":
        return SymbolFilters(self.symbol, self.tick_size, self.step_size, self.min_qty, self.max_qty,
                             self.market_step_size, self.market_max_qty, self.min_notional,
                             max_leverage, mmr, cum)


def parse_symbol_filters(info: dict) -> SymbolFilters | None:
    f = {x["filterType"]: x for x in info.get("filters", [])}
    try:
        lot = f["LOT_SIZE"]
        mlot = f.get("MARKET_LOT_SIZE", lot)
        return SymbolFilters(
            symbol=info["symbol"],
            tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
            step_size=Decimal(lot["stepSize"]),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            market_step_size=Decimal(mlot["stepSize"]),
            market_max_qty=Decimal(mlot["maxQty"]),
            min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
        )
    except (KeyError, ValueError):
        return None


def eligible_perpetuals(exchange_info: dict, settings: Settings) -> dict[str, SymbolFilters]:
    out: dict[str, SymbolFilters] = {}
    for s in exchange_info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
            continue
        if s["symbol"] in settings.blacklist:
            continue
        flt = parse_symbol_filters(s)
        if flt and flt.tick_size > 0 and flt.step_size > 0:
            out[s["symbol"]] = flt
    return out


def apply_brackets(filters: dict[str, SymbolFilters], brackets: list, max_leverage: int) -> dict[str, SymbolFilters]:
    """İlk bracket: sembolün desteklediği maksimum kaldıraç ve maintenance margin oranı."""
    out = dict(filters)
    for b in brackets:
        sym = b.get("symbol")
        if sym not in out or not b.get("brackets"):
            continue
        first = b["brackets"][0]
        exch_max = int(first.get("initialLeverage", max_leverage))
        out[sym] = out[sym].with_bracket(min(exch_max, max_leverage), float(first.get("maintMarginRatio", 0.004)),
                                         float(first.get("cum", 0.0)))
    return out


def book_metrics(depth: dict, band_pct: float = 0.005) -> tuple[float, float]:
    """(spread_pct, ±band içindeki toplam USDT derinlik)."""
    bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
    if not bids or not asks:
        return 1.0, 0.0
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread = (best_ask - best_bid) / mid
    lo, hi = mid * (1 - band_pct), mid * (1 + band_pct)
    depth_usdt = sum(p * q for p, q in bids if p >= lo) + sum(p * q for p, q in asks if p <= hi)
    return spread, depth_usdt


async def build_universe(trading: BinanceREST, public: BinanceREST | None, settings: Settings,
                         filters: dict[str, SymbolFilters]) -> list[tuple[str, float]]:
    """Hacme göre sıralı universe (sembol, 24h quote volume). Spread/derinlik filtresi
    işlemin gerçekleştiği (testnet) order book üzerinde uygulanır."""
    if settings.whitelist:
        candidates = [(s, 0.0) for s in settings.whitelist if s in filters]
    else:
        src = public if (settings.universe_volume_source == "public" and public) else trading
        tickers = await src.ticker_24h()
        vols = [(t["symbol"], float(t.get("quoteVolume", 0))) for t in tickers if t["symbol"] in filters]
        vols = [v for v in vols if v[1] >= settings.min_24h_quote_volume]
        vols.sort(key=lambda x: x[1], reverse=True)
        candidates = vols[: settings.universe_size * 2]
    selected: list[tuple[str, float]] = []
    for sym, vol in candidates:
        try:
            spread, depth = book_metrics(await trading.depth(sym, 50))
        except Exception as exc:
            log.warning("depth alınamadı %s: %r", sym, exc)
            continue
        if spread > settings.max_spread_pct * 2 or depth < settings.min_book_depth_usdt:
            continue
        selected.append((sym, vol))
        if len(selected) >= settings.universe_size:
            break
    return selected
