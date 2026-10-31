"""Fear & Greed index (yalnızca makro bağlam). Hata durumunda None döner;
fallback davranışı config'deki fng_fallback_points ile yönetilir."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class FearGreedProvider:
    def __init__(self, url: str, ttl_s: float = 1800, client: httpx.AsyncClient | None = None):
        self.url = url
        self.ttl = ttl_s
        self._client = client or httpx.AsyncClient(timeout=8)
        self.value: int | None = None
        self.fetched_at = 0.0

    async def get(self) -> int | None:
        if self.value is not None and time.monotonic() - self.fetched_at < self.ttl:
            return self.value
        try:
            r = await self._client.get(self.url)
            r.raise_for_status()
            self.value = int(r.json()["data"][0]["value"])
            self.fetched_at = time.monotonic()
        except Exception as exc:
            log.warning("Fear&Greed alınamadı: %r", exc)
            if time.monotonic() - self.fetched_at > self.ttl * 4:
                self.value = None
        return self.value
