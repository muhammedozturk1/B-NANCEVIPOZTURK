"""İsteğe bağlı CoinGlass entegrasyonu (API v4).

COINGLASS_API_KEY tanımlıysa, kurulum bulunan coinler için likidasyon ısı haritası
(model2) çekilir ve hedef seçiminde tahmini kümelerin yerine/yanına eklenir.
Anahtar yoksa bot tamamen ücretsiz Binance verisiyle çalışır.
Not: Hangi uç noktaların hangi pakette olduğu CoinGlass tarafından belirlenir;
paketin izin vermediği durumda bot sessizce ücretsiz veriye döner.
"""
from __future__ import annotations

import time

import aiohttp

from . import config

BASE = "https://open-api-v4.coinglass.com"
_cache: dict[str, tuple[float, list, list]] = {}
_disabled_reason = ""


def enabled() -> bool:
    return bool(config.COINGLASS_API_KEY) and not _disabled_reason


def status() -> str:
    if not config.COINGLASS_API_KEY:
        return "kapalı (anahtar yok, ücretsiz Binance verisi kullanılıyor)"
    if _disabled_reason:
        return f"devre dışı: {_disabled_reason}"
    return "açık"


async def liq_clusters(symbol: str, price: float, top: int = 6) -> tuple[list, list]:
    """(üstteki kümeler, alttaki kümeler) -> [(fiyat, ağırlık)]"""
    global _disabled_reason
    if not enabled():
        return [], []
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < 300:
        return hit[1], hit[2]
    params = {"exchange": "Binance", "symbol": symbol, "range": "3d"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.get(f"{BASE}/api/futures/liquidation/heatmap/model2", params=params,
                             headers={"CG-API-KEY": config.COINGLASS_API_KEY, "accept": "application/json"}) as r:
                data = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return [], []
    if str(data.get("code")) != "0":
        msg = str(data.get("msg", ""))
        if any(w in msg.lower() for w in ("upgrade", "plan", "permission", "key", "invalid")):
            _disabled_reason = msg[:120]
        return [], []
    d = data.get("data") or {}
    y = d.get("y_axis") or d.get("yAxis") or []
    pts = d.get("liquidation_leverage_data") or d.get("liq") or []
    sums: dict[int, float] = {}
    for row in pts:
        try:
            yi, val = int(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        sums[yi] = sums.get(yi, 0.0) + val
    levels = []
    for yi, v in sums.items():
        if 0 <= yi < len(y):
            try:
                levels.append((float(y[yi]), v))
            except (TypeError, ValueError):
                pass
    above = sorted([lv for lv in levels if lv[0] > price], key=lambda x: -x[1])[:top]
    below = sorted([lv for lv in levels if lv[0] < price], key=lambda x: -x[1])[:top]
    above.sort()
    below.sort(reverse=True)
    _cache[symbol] = (time.time(), above, below)
    return above, below
