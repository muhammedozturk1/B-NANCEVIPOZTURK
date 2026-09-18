"""
Dinamik Tarama Listesi Sağlayıcı.

Motorların "hangi coin'lere bakayım" sorusuna cevap verir. Sabit bir liste
YOKTUR - Binance Futures'taki en yüksek hacimli/likit pariteler otomatik
seçilir. Her motor döngüsünde yeniden hesaplamak gereksiz API yükü
yaratacağı için, liste SYMBOL_CACHE_TTL_SECONDS süreyle önbelleğe alınır.
"""
import logging
import time
from backend import config

logger = logging.getLogger("symbol_scanner")

_cache = {"symbols": None, "timestamp": 0}


def get_active_symbols(client) -> list:
    now = time.time()

    if _cache["symbols"] is not None and (now - _cache["timestamp"]) < config.SYMBOL_CACHE_TTL_SECONDS:
        return _cache["symbols"]

    try:
        symbols = client.fetch_top_symbols(
            limit=config.TOP_SYMBOLS_COUNT,
            min_volume=config.MIN_24H_VOLUME_USDT,
            quote=config.QUOTE_CURRENCY,
            exclude_base_assets=config.EXCLUDE_BASE_ASSETS,
        )
        if symbols:
            _cache["symbols"] = symbols
            _cache["timestamp"] = now
            logger.info(f"🔍 Dinamik tarama listesi güncellendi ({len(symbols)} parite): {symbols}")
            return symbols
        else:
            logger.warning("Tarama listesi boş döndü, önceki liste veya fallback kullanılacak.")
    except Exception as e:
        logger.error(f"Tarama listesi alınamadı: {e}")

    return _cache["symbols"] or config.FALLBACK_SYMBOLS
