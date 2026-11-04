"""Korku & Açgözlülük endeksi (alternative.me). Alınamazsa None döner ve filtre uygulanmaz."""
from __future__ import annotations

import time

import aiohttp

URL = "https://api.alternative.me/fng/"
_latest: tuple[float, int | None] = (0.0, None)


async def _get(limit: int) -> list[dict]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(URL, params={"limit": str(limit), "format": "json"}) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
            return data.get("data") or []


async def latest(offline: bool = False) -> int | None:
    """Güncel değer (1 saat önbellek)."""
    global _latest
    if offline:
        return None
    if time.time() - _latest[0] < 3600 and _latest[1] is not None:
        return _latest[1]
    try:
        rows = await _get(1)
        v = int(rows[0]["value"])
        _latest = (time.time(), v)
        return v
    except Exception:  # noqa: BLE001
        return _latest[1] if time.time() - _latest[0] < 6 * 3600 else None


async def history(offline: bool = False) -> dict[int, int]:
    """Gün indisi (UTC ms // 86400000) -> değer. Geçmiş test için."""
    if offline:
        return {}
    try:
        return {int(r["timestamp"]) * 1000 // 86_400_000: int(r["value"]) for r in await _get(0)}
    except Exception:  # noqa: BLE001
        return {}
