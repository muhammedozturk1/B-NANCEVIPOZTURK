"""
Korku-Açgözlülük Endeksi (Fear & Greed Index) - Swing motoru için makro filtre.

Mantık (kontraryan yaklaşım): Piyasa aşırı korkuluysa (herkes satıyor) genelde
dip bölgesine yakınızdır -> long bias. Aşırı açgözlülükte (herkes alıyor)
tepe bölgesine yakınızdır -> short bias. Orta bölgede nötr, filtre işlem
açılmasını engellemez.
"""
import logging
import requests
from backend import config

logger = logging.getLogger("fear_greed")

_cache = {"value": None, "timestamp": None}
_CACHE_TTL_SECONDS = 3600  # endeks günde 1 kez güncellenir, sık API çağrısına gerek yok


def fetch_index() -> int:
    """API'den güncel korku-açgözlülük değerini (0-100) çeker, cache'ler."""
    import time
    now = time.time()

    if _cache["value"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
        return _cache["value"]

    try:
        response = requests.get(config.FEAR_GREED_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        value = int(data["data"][0]["value"])
        _cache["value"] = value
        _cache["timestamp"] = now
        return value
    except Exception as e:
        logger.error(f"Korku-Açgözlülük endeksi çekilemedi: {e}")
        return _cache["value"] if _cache["value"] is not None else 50  # varsayılan nötr


def get_signal(ohlcv: list = None) -> dict:
    """ohlcv parametresi diğer stratejilerle aynı arayüzü korumak için var, kullanılmıyor."""
    value = fetch_index()

    if value <= config.FEAR_GREED_EXTREME_FEAR:
        direction = "long"
        classification = "Aşırı Korku"
    elif value >= config.FEAR_GREED_EXTREME_GREED:
        direction = "short"
        classification = "Aşırı Açgözlülük"
    else:
        direction = "neutral"
        classification = "Nötr"

    return {"direction": direction, "value": value, "classification": classification}
