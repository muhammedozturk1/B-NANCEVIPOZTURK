"""Gerçek Binance (mainnet) PUBLIC verisi: fonlama oranları, spot ve vadeli fiyatlar.
API key gerekmez. Hem canlı motor hem backtest kullanır."""
import logging
from backend import config

logger = logging.getLogger("funding_market")


class FundingMarket:
    def __init__(self):
        import ccxt
        self.fut = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
        self.spot = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self.fut.load_markets()
        self.spot.load_markets()
        self.spot_symbols = {
            s for s, m in self.spot.markets.items()
            if m.get("spot") and m.get("active", True) and m.get("quote") == config.QUOTE_CURRENCY
        }

    @staticmethod
    def perp(symbol: str) -> str:
        return f"{symbol}:{config.QUOTE_CURRENCY}"   # BTC/USDT -> BTC/USDT:USDT

    def candidates(self, limit: int = None) -> list:
        """Likit, gerçek kripto, hem spot hem vadelisi olan pariteler."""
        from backend.exchange import universe
        tickers = self.fut.fetch_tickers()
        return universe.select_symbols(self.fut, tickers, limit or config.TOP_SYMBOLS_COUNT,
                                       config.MIN_24H_VOLUME_USDT, allowed=self.spot_symbols)

    def current_rates(self) -> dict:
        """{ 'BTC/USDT': güncel (bir sonraki) fonlama oranı }"""
        out = {}
        for raw, fr in self.fut.fetch_funding_rates().items():
            rate = fr.get("fundingRate")
            if rate is not None and raw.endswith(f":{config.QUOTE_CURRENCY}"):
                out[raw.split(":")[0]] = float(rate)
        return out

    def history(self, symbol: str, since_ms: int) -> list:
        """[(ts_ms, rate, mark_price_or_None), ...] since_ms'den itibaren (sayfalı)."""
        rows, start = [], since_ms
        while True:
            batch = self.fut.fetch_funding_rate_history(self.perp(symbol), since=start, limit=1000)
            if not batch:
                break
            for b in batch:
                mark = (b.get("info") or {}).get("markPrice")
                rows.append((int(b["timestamp"]), float(b["fundingRate"]),
                             float(mark) if mark not in (None, "", "0") else None))
            if len(batch) < 1000:
                break
            start = int(batch[-1]["timestamp"]) + 1
        rows.sort()
        return rows

    def prices(self, symbol: str):
        """(spot_son_fiyat, vadeli_son_fiyat)"""
        return float(self.spot.fetch_ticker(symbol)["last"]), float(self.fut.fetch_ticker(self.perp(symbol))["last"])
