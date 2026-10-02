"""
Piyasa evreni: Binance Futures'taki TÜM USDT perpetual kontratları taranır,
sabit bir coin listesi yoktur. Sadece şu kurallarla elenir:

  1) Gerçek kripto olmalı: altın (XAU), gümüş, petrol, hisse senedi (TSLA, SNDK...)
     gibi "TradFi" kontratları ve isimsiz/sembolik meme kontratları elenir.
  2) Yeterince likit olmalı: 24s hacim >= MIN_24H_VOLUME_USDT
     (sığ coinlerde stop'lar kayma ile vuruluyor - MOVR örneği).
  3) Yeterince eski olmalı: en az MIN_LISTING_DAYS gündür listede
     (yeni listelenen coinler pump/dump yapar, modelin öğreneceği geçmişi yok).

Aynı fonksiyon hem canlı bot hem AI eğitimi tarafından kullanılır.
"""
import time
from backend import config


def is_crypto_perp(market: dict) -> bool:
    if not market or not market.get("swap") or market.get("quote") != config.QUOTE_CURRENCY:
        return False
    if not market.get("active", True):
        return False
    info = market.get("info", {}) or {}

    contract_type = info.get("contractType")
    if contract_type and contract_type != "PERPETUAL":
        return False  # TRADIFI_PERPETUAL vb.
    underlying = info.get("underlyingType")
    if underlying and str(underlying).upper() != "COIN":
        return False  # emtia, hisse, endeks
    sub_types = " ".join(str(x) for x in (info.get("underlyingSubType") or [])).lower()
    if any(word in sub_types for word in ("tradfi", "stock", "commodity", "index")):
        return False

    base = market.get("base", "")
    if not base or not base.isascii() or not base.isalnum():
        return False  # örn. '龙虾'
    if base in config.EXCLUDE_BASE_ASSETS or base in config.NON_CRYPTO_BLACKLIST:
        return False

    onboard = info.get("onboardDate")
    if onboard:
        age_days = (time.time() * 1000 - int(onboard)) / 86_400_000
        if age_days < config.MIN_LISTING_DAYS:
            return False
    return True


def select_symbols(exchange, tickers: dict, limit: int, min_volume: float, allowed: set = None) -> list:
    """Filtreleri geçen pariteleri 24s hacme göre sıralı döner ('BTC/USDT' biçiminde)."""
    rows, seen = [], set()
    for raw_symbol, ticker in tickers.items():
        market = exchange.markets.get(raw_symbol)
        if not is_crypto_perp(market):
            continue
        symbol = f"{market['base']}/{market['quote']}"
        if symbol in seen or (allowed is not None and symbol not in allowed):
            continue
        volume = ticker.get("quoteVolume") or 0
        if volume < min_volume:
            continue
        seen.add(symbol)
        rows.append((symbol, volume))
    rows.sort(key=lambda r: -r[1])
    return [s for s, _ in rows[:limit]]
