"""Binance USDⓈ-M Futures istemcisi (REST + WebSocket).

Kasım 2026 itibarıyla geçerli kurallar:
- STOP_MARKET / TAKE_PROFIT_MARKET emirleri artık /fapi/v1/order ile değil,
  Algo Order servisi (/fapi/v1/algoOrder) ile gönderilir (2025-12'den beri).
- WebSocket akışları /public, /market, /private rotalarına ayrıldı
  (eski /ws ve /stream adresleri 2026-04-23'te kapatıldı).
- ABD IP'leri HTTP 451 ile engellenir -> sunucu Frankfurt/Singapur olmalı.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Awaitable, Callable

import aiohttp

from . import config


class BinanceError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"Binance hata {status} / {code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg


class GeoBlockedError(BinanceError):
    pass


@dataclass
class SymbolInfo:
    symbol: str
    base: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    market_step: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal

    def round_price(self, price: float, mode: str = "nearest") -> float:
        d = Decimal(str(price))
        rounding = ROUND_HALF_UP if mode == "nearest" else ROUND_DOWN
        q = (d / self.tick).quantize(Decimal(1), rounding=rounding) * self.tick
        return float(q)

    def round_qty(self, qty: float) -> float:
        step = max(self.step, self.market_step)
        d = Decimal(str(qty))
        q = (d / step).quantize(Decimal(1), rounding=ROUND_DOWN) * step
        if self.market_max_qty > 0:
            q = min(q, self.market_max_qty)
        return float(q)

    def fmt_price(self, price: float) -> str:
        return _fmt(Decimal(str(self.round_price(price))), self.tick)

    def fmt_qty(self, qty: float) -> str:
        return _fmt(Decimal(str(self.round_qty(qty))), max(self.step, self.market_step))


def _fmt(value: Decimal, step: Decimal) -> str:
    exp = max(0, -step.normalize().as_tuple().exponent)
    return f"{value:.{exp}f}"


class BinanceREST:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = ""):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.secret = api_secret.encode()
        self._session: aiohttp.ClientSession | None = None
        self._time_offset = 0
        self._last_sync = 0.0
        self.used_weight = 0

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise BinanceError(0, None, "API anahtarı tanımlı değil")
            if time.time() - self._last_sync > 1800:
                await self.sync_time()
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            params.setdefault("recvWindow", 5000)
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={sig}"
            headers["X-MBX-APIKEY"] = self.key
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        s = await self.session()
        for attempt in range(3):
            try:
                async with s.request(method, url, headers=headers) as resp:
                    w = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                    if w:
                        self.used_weight = int(w)
                    text = await resp.text()
                    if resp.status == 451:
                        raise GeoBlockedError(451, None, "Binance bu sunucu konumunu engelliyor (restricted location)")
                    if resp.status in (418, 429):
                        retry = int(resp.headers.get("Retry-After", "5"))
                        raise BinanceError(resp.status, None, f"Hız sınırı aşıldı, {retry} sn bekle")
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        raise BinanceError(resp.status, None, text[:200])
                    if resp.status >= 400:
                        code = data.get("code") if isinstance(data, dict) else None
                        msg = data.get("msg", text[:200]) if isinstance(data, dict) else text[:200]
                        if code == -1021 and attempt == 0:  # zaman senkronu bozuk
                            await self.sync_time()
                            continue
                        raise BinanceError(resp.status, code, msg)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 2:
                    raise BinanceError(0, None, f"Ağ hatası: {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
        raise BinanceError(0, None, "İstek başarısız")

    async def sync_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time")
        self._time_offset = int(data["serverTime"]) - int(time.time() * 1000)
        self._last_sync = time.time()

    # ---------- Piyasa verisi (anahtarsız) ----------
    async def exchange_info(self) -> dict[str, SymbolInfo]:
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        out: dict[str, SymbolInfo] = {}
        for s in data.get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            f = {x["filterType"]: x for x in s.get("filters", [])}
            try:
                out[s["symbol"]] = SymbolInfo(
                    symbol=s["symbol"],
                    base=s.get("baseAsset", ""),
                    tick=Decimal(f["PRICE_FILTER"]["tickSize"]).normalize(),
                    step=Decimal(f["LOT_SIZE"]["stepSize"]).normalize(),
                    min_qty=Decimal(f["LOT_SIZE"]["minQty"]),
                    market_step=Decimal(f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])["stepSize"]).normalize(),
                    market_min_qty=Decimal(f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])["minQty"]),
                    market_max_qty=Decimal(f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])["maxQty"]),
                    min_notional=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5")),
                )
            except (KeyError, ArithmeticError):
                continue
        return out

    async def ticker_24h(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def klines(self, symbol: str, interval: str, limit: int = 300,
                     start: int | None = None, end: int | None = None) -> list[list]:
        return await self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit, "startTime": start, "endTime": end})

    async def klines_range(self, symbol: str, interval: str, start: int, end: int) -> list[list]:
        """Uzun aralıklar için sayfalı mum çekme (geçmiş test)."""
        step = config.INTERVAL_MS[interval]
        out: list[list] = []
        cur = start
        while cur < end:
            batch = await self.klines(symbol, interval, limit=1500, start=cur, end=end)
            if not batch:
                break
            out.extend(batch)
            nxt = int(batch[-1][0]) + step
            if nxt <= cur:
                break
            cur = nxt
            await asyncio.sleep(0.15)
        # tekrarları at
        seen, uniq = set(), []
        for k in out:
            if k[0] not in seen:
                seen.add(k[0])
                uniq.append(k)
        return uniq

    async def premium_index(self) -> list[dict]:
        return await self._request("GET", "/fapi/v1/premiumIndex")

    async def open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 30) -> list[dict]:
        return await self._request("GET", "/futures/data/openInterestHist",
                                   {"symbol": symbol, "period": period, "limit": limit})

    async def depth(self, symbol: str, limit: int = 100) -> dict:
        return await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    # ---------- Hesap / işlem (imzalı) ----------
    async def balance_usdt(self) -> dict:
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        for a in data:
            if a.get("asset") == "USDT":
                return {"wallet": float(a.get("balance", 0)), "available": float(a.get("availableBalance", 0)),
                        "upnl": float(a.get("crossUnPnl", 0))}
        return {"wallet": 0.0, "available": 0.0, "upnl": 0.0}

    async def position_mode_is_hedge(self) -> bool:
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    async def positions(self) -> list[dict]:
        data = await self._request("GET", "/fapi/v3/positionRisk", signed=True)
        return [p for p in data if abs(float(p.get("positionAmt", 0))) > 0]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_isolated(self, symbol: str) -> None:
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type"
                raise

    async def market_order(self, symbol: str, side: str, qty: str, reduce_only: bool = False, client_id: str | None = None) -> dict:
        return await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
            "reduceOnly": "true" if reduce_only else None,
            "newClientOrderId": client_id, "newOrderRespType": "RESULT"}, signed=True)

    async def algo_close_order(self, symbol: str, side: str, order_type: str, trigger_price: str,
                               client_algo_id: str | None = None) -> dict:
        """Pozisyonun tamamını kapatan koşullu emir (STOP_MARKET / TAKE_PROFIT_MARKET)."""
        return await self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
            "triggerPrice": trigger_price, "closePosition": "true", "workingType": "MARK_PRICE",
            "priceProtect": "true", "clientAlgoId": client_algo_id}, signed=True)

    async def cancel_algo(self, algo_id: int) -> None:
        try:
            await self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)
        except BinanceError as e:
            if e.code not in (-2011, -2013):  # zaten yok
                raise

    async def cancel_all_algo(self, symbol: str) -> None:
        try:
            await self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
        except BinanceError:
            pass

    async def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    async def user_trades(self, symbol: str, start: int) -> list[dict]:
        return await self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start, "limit": 500}, signed=True)


def parse_klines(raw: list[list]) -> dict[str, list]:
    """Binance kline -> sütunlar. taker_buy = alıcı agresif hacim (CVD için)."""
    return {
        "time": [int(k[0]) for k in raw],
        "open": [float(k[1]) for k in raw],
        "high": [float(k[2]) for k in raw],
        "low": [float(k[3]) for k in raw],
        "close": [float(k[4]) for k in raw],
        "volume": [float(k[5]) for k in raw],
        "close_time": [int(k[6]) for k in raw],
        "quote_volume": [float(k[7]) for k in raw],
        "taker_buy": [float(k[9]) for k in raw],
    }


class MarketStream:
    """Tüm piyasa mark fiyatı (1 sn) + tüm likidasyonlar tek bağlantıda."""

    def __init__(self, url: str, streams: list[str], on_message: Callable[[str, Any], Awaitable[None] | None]):
        self.url = f"{url}?streams=" + "/".join(streams)
        self.on_message = on_message
        self.connected = False
        self.last_msg = 0.0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run(self, log: Callable[[str, str], None]) -> None:
        backoff = 2
        while not self._stop:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(self.url, heartbeat=30) as ws:
                        self.connected = True
                        backoff = 2
                        log("info", "WebSocket bağlandı (mark fiyatı + likidasyonlar)")
                        opened = time.time()
                        async for msg in ws:
                            if self._stop:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.last_msg = time.time()
                                payload = json.loads(msg.data)
                                res = self.on_message(payload.get("stream", ""), payload.get("data"))
                                if asyncio.iscoroutine(res):
                                    await res
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                            # Binance bağlantıları 24 saatte kapatır; 23 saatte kendimiz yenileyelim
                            if time.time() - opened > 23 * 3600:
                                break
            except Exception as e:  # noqa: BLE001
                log("warn", f"WebSocket koptu: {e}")
            self.connected = False
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
