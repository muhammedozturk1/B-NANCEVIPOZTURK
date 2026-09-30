"""
Haber/Makro Takvim Filtresi.

FOMC, CPI, NFP gibi yüksek etkili USD haberleri sırasında volatilite anormal
olur. Bu modül, bu tür haberlerden belirli bir süre önce/sonra YENİ işlem
açılmasını engeller (mevcut açık işlemleri etkilemez).

Veri kaynağı: ForexFactory'nin herkese açık haftalık takvim JSON'u.
"""
import logging
import time
import requests
from datetime import datetime, timedelta, timezone
from backend import config

logger = logging.getLogger("news_filter")

_cache = {"events": None, "timestamp": 0}
_CACHE_TTL_SECONDS = 3600 * 6  # takvim sık değişmez, 6 saatte bir tekrar çekilsin


def _fetch_calendar() -> list:
    now = time.time()
    if _cache["events"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
        return _cache["events"]

    try:
        response = requests.get(config.NEWS_CALENDAR_URL, timeout=10)
        response.raise_for_status()
        events = response.json()
        _cache["events"] = events
        _cache["timestamp"] = now
        return events
    except Exception as e:
        logger.error(f"Haber takvimi çekilemedi: {e}")
        return _cache["events"] if _cache["events"] is not None else []


def is_news_blackout_active() -> bool:
    """Şu an yüksek etkili bir USD haberinin öncesi/sonrası penceresinde miyiz?"""
    events = _fetch_calendar()
    if not events:
        return False  # takvim çekilemediyse işlemi engelleme, sessizce geç

    now = datetime.now(timezone.utc)
    before = timedelta(minutes=config.NEWS_BLACKOUT_MINUTES_BEFORE)
    after = timedelta(minutes=config.NEWS_BLACKOUT_MINUTES_AFTER)

    for event in events:
        try:
            if config.NEWS_HIGH_IMPACT_ONLY and event.get("impact", "").lower() != "high":
                continue
            if event.get("country") not in config.NEWS_RELEVANT_CURRENCIES:
                continue

            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            if (event_time - before) <= now <= (event_time + after):
                logger.info(f"📰 Haber blackout aktif: {event.get('title')} ({event_time})")
                return True
        except (KeyError, ValueError):
            continue  # beklenmeyen formatta bir event varsa atla, botu durdurma

    return False
