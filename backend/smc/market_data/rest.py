"""Binance USDⓈ-M Futures REST istemcisi.

- HMAC-SHA256 imzalama
- REQUEST_WEIGHT ve ORDER limitlerine göre token-bucket rate limiter
  (Binance response header'ları ile senkronize edilir; sabit sleep yok)
- 429 / 418 / 5xx / timeout için exponential backoff
- Server time senkronizasyonu ve -1021 timestamp hatasında otomatik resync
- Emirlerde duplicate koruması: timeout sonrası clientOrderId ile sorgulayıp
  emir yoksa yeniden gönderir (aynı emri iki kez açmaz)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int | None, msg: str, path: str = ""):
        super().__init__(f"[{status}] {code}: {msg} ({path})")
        self.status = status
        self.code = code
        self.msg = msg
        self.path = path

    @property
    def retryable(self) -> bool:
        return self.status in (429, 418) or self.status >= 500 or self.code in (-1001, -1003, -1007)


# Bilinen "zararsız" hata kodları
CODE_NO_NEED_MARGIN_TYPE = -4046
CODE_NO_NEED_POSITION_MODE = -4059
CODE_ORDER_NOT_FOUND = (-2011, -2013)
CODE_TIMESTAMP = -1021
CODE_WOULD_TRIGGER = -2021
CODE_UNKNOWN_EXEC = -1007


class SlidingWindowLimiter:
    """Belirli bir pencere içindeki toplam ağırlığı sınırlar (async, kilitli)."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window = window_s
        self.events: deque[tuple[float, int]] = deque()
        self.used = 0
        self._lock = asyncio.Lock()

    def _purge(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= self.window:
            _, w = self.events.popleft()
            self.used -= w

    async def acquire(self, weight: int = 1) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._purge(now)
                if self.used + weight <= self.limit:
                    self.events.append((now, weight))
                    self.used += weight
                    return
                wait = self.window - (now - self.events[0][0]) + 0.01
                await asyncio.sleep(max(wait, 0.01))

    def sync_from_header(self, server_used: int) -> None:
        """Sunucunun bildirdiği kullanım bizimkinden yüksekse farkı ekle."""
        now = time.monotonic()
        self._purge(now)
        if server_used > self.used:
            extra = server_used - self.used
            self.events.append((now, extra))
            self.used += extra


@dataclass
class TimeSync:
    offset_ms: int = 0
    last_sync: float = 0.0
    ok: bool = False

    def timestamp(self) -> int:
        return int(time.time() * 1000) + self.offset_ms


class BinanceREST:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "", *, recv_window: int = 5000,
                 weight_per_min: int = 2000, orders_per_10s: int = 250, orders_per_min: int = 1000,
                 timeout: float = 10.0, max_retries: int = 4, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.recv_window = recv_window
        self.max_retries = max_retries
        self.time = TimeSync()
        self.weight = SlidingWindowLimiter(weight_per_min, 60)
        self.orders_10s = SlidingWindowLimiter(orders_per_10s, 10)
        self.orders_1m = SlidingWindowLimiter(orders_per_min, 60)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.banned_until: float = 0.0
        self.consecutive_failures = 0

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core
    def _sign(self, params: dict) -> str:
        query = urlencode(params, doseq=True)
        sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    async def sync_time(self) -> int:
        t0 = time.time() * 1000
        data = await self.request("GET", "/fapi/v1/time", weight=1)
        t1 = time.time() * 1000
        server = int(data["serverTime"])
        self.time.offset_ms = int(server - (t0 + t1) / 2)
        self.time.last_sync = time.monotonic()
        self.time.ok = True
        return self.time.offset_ms

    async def request(self, method: str, path: str, params: dict | None = None, *, signed: bool = False,
                      weight: int = 1, is_order: bool = False, retries: int | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = self.max_retries if retries is None else retries
        last_exc: Exception | None = None
        resynced = False
        for attempt in range(attempts + 1):
            if time.monotonic() < self.banned_until:
                raise BinanceAPIError(418, None, "IP geçici olarak banlı, bekleniyor", path)
            await self.weight.acquire(weight)
            if is_order:
                await self.orders_10s.acquire(1)
                await self.orders_1m.acquire(1)
            headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
            if signed:
                p = dict(params)
                p["timestamp"] = self.time.timestamp()
                p["recvWindow"] = self.recv_window
                query = self._sign(p)
            else:
                query = urlencode(params, doseq=True)
            url = f"{self.base_url}{path}"
            if query:
                url = f"{url}?{query}"
            try:
                resp = await self._client.request(method, url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                self.consecutive_failures += 1
                if is_order:  # emir durumu bilinmiyor: çağıran taraf idempotent kontrol yapar
                    raise BinanceAPIError(0, CODE_UNKNOWN_EXEC, f"transport: {exc!r}", path) from exc
                await self._backoff(attempt)
                continue
            self._track_headers(resp)
            if resp.status_code == 200:
                self.consecutive_failures = 0
                return resp.json()
            err = self._parse_error(resp, path)
            if err.code == CODE_TIMESTAMP and not resynced:
                resynced = True
                try:
                    await self.sync_time()
                except Exception:  # pragma: no cover
                    pass
                continue
            if resp.status_code in (418, 429):
                retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                if resp.status_code == 418:
                    self.banned_until = time.monotonic() + max(retry_after, 120)
                    raise err
                await asyncio.sleep(max(retry_after, 2 ** attempt))
                last_exc = err
                continue
            if resp.status_code >= 500:
                self.consecutive_failures += 1
                if is_order:  # 5xx'te emir yerleşmiş olabilir
                    raise err
                last_exc = err
                await self._backoff(attempt)
                continue
            raise err  # 4xx: retry edilmez
        assert last_exc is not None
        raise last_exc

    async def _backoff(self, attempt: int) -> None:
        delay = min(30.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.25)
        await asyncio.sleep(delay)

    def _track_headers(self, resp: httpx.Response) -> None:
        for k, v in resp.headers.items():
            kl = k.lower()
            try:
                if kl == "x-mbx-used-weight-1m":
                    self.weight.sync_from_header(int(v))
                elif kl == "x-mbx-order-count-10s":
                    self.orders_10s.sync_from_header(int(v))
                elif kl == "x-mbx-order-count-1m":
                    self.orders_1m.sync_from_header(int(v))
            except ValueError:
                pass

    @staticmethod
    def _parse_error(resp: httpx.Response, path: str) -> BinanceAPIError:
        try:
            body = resp.json()
            return BinanceAPIError(resp.status_code, body.get("code"), body.get("msg", resp.text), path)
        except Exception:
            return BinanceAPIError(resp.status_code, None, resp.text[:300], path)

    # ---------------------------------------------------------------- public
    async def exchange_info(self) -> dict:
        return await self.request("GET", "/fapi/v1/exchangeInfo", weight=1)

    async def klines(self, symbol: str, interval: str, limit: int = 500, start_time: int | None = None,
                     end_time: int | None = None) -> list:
        w = 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
        return await self.request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                  "limit": limit, "startTime": start_time, "endTime": end_time}, weight=w)

    async def ticker_24h(self) -> list:
        return await self.request("GET", "/fapi/v1/ticker/24hr", weight=40)

    async def book_ticker(self, symbol: str) -> dict:
        return await self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, weight=2)

    async def depth(self, symbol: str, limit: int = 50) -> dict:
        w = 2 if limit <= 50 else 5 if limit <= 100 else 10
        return await self.request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight=w)

    async def premium_index(self, symbol: str | None = None) -> Any:
        return await self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, weight=1 if symbol else 10)

    async def open_interest(self, symbol: str) -> dict:
        return await self.request("GET", "/fapi/v1/openInterest", {"symbol": symbol}, weight=1)

    async def funding_rate_history(self, symbol: str, start_time: int | None = None, limit: int = 1000) -> list:
        return await self.request("GET", "/fapi/v1/fundingRate",
                                  {"symbol": symbol, "startTime": start_time, "limit": limit}, weight=1)

    # --------------------------------------------------------------- account
    async def leverage_brackets(self) -> list:
        return await self.request("GET", "/fapi/v1/leverageBracket", signed=True, weight=1)

    async def account(self) -> dict:
        return await self.request("GET", "/fapi/v2/account", signed=True, weight=5)

    async def position_risk(self, symbol: str | None = None) -> list:
        return await self.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True, weight=5)

    async def open_orders(self, symbol: str | None = None) -> list:
        return await self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True,
                                  weight=1 if symbol else 40)

    async def open_algo_orders(self, symbol: str | None = None) -> list:
        data = await self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True,
                                  weight=1 if symbol else 40)
        if isinstance(data, dict):  # bazı sürümler {"orders": [...]} döndürür
            return data.get("orders", data.get("rows", []))
        return data

    async def user_trades(self, symbol: str, start_time: int | None = None, limit: int = 1000) -> list:
        return await self.request("GET", "/fapi/v1/userTrades",
                                  {"symbol": symbol, "startTime": start_time, "limit": limit}, signed=True, weight=5)

    async def income(self, symbol: str | None = None, income_type: str | None = None,
                     start_time: int | None = None, limit: int = 1000) -> list:
        return await self.request("GET", "/fapi/v1/income", {"symbol": symbol, "incomeType": income_type,
                                  "startTime": start_time, "limit": limit}, signed=True, weight=30)

    # ------------------------------------------------------------- settings
    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        try:
            await self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type},
                               signed=True)
        except BinanceAPIError as e:
            if e.code != CODE_NO_NEED_MARGIN_TYPE:
                raise

    async def set_one_way_mode(self) -> None:
        try:
            await self.request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, signed=True)
        except BinanceAPIError as e:
            if e.code != CODE_NO_NEED_POSITION_MODE:
                raise

    async def get_position_mode(self) -> bool:
        data = await self.request("GET", "/fapi/v1/positionSide/dual", signed=True, weight=30)
        return bool(data.get("dualSidePosition"))

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    # ---------------------------------------------------------------- orders
    async def new_order(self, **params: Any) -> dict:
        return await self.request("POST", "/fapi/v1/order", params, signed=True, is_order=True, retries=0)

    async def query_order(self, symbol: str, client_id: str) -> dict:
        return await self.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id},
                                  signed=True, weight=1)

    async def cancel_order(self, symbol: str, client_id: str) -> dict:
        return await self.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id},
                                  signed=True)

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    async def new_algo_order(self, **params: Any) -> dict:
        params.setdefault("algoType", "CONDITIONAL")
        return await self.request("POST", "/fapi/v1/algoOrder", params, signed=True, is_order=True, retries=0)

    async def query_algo_order(self, client_algo_id: str) -> dict:
        return await self.request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id}, signed=True)

    async def cancel_algo_order(self, client_algo_id: str) -> dict:
        return await self.request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id}, signed=True)

    async def cancel_all_algo_orders(self, symbol: str) -> dict:
        return await self.request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)

    # ----------------------------------------------------------- user stream
    async def create_listen_key(self) -> str:
        data = await self.request("POST", "/fapi/v1/listenKey", weight=1)
        return data["listenKey"]

    async def keepalive_listen_key(self) -> None:
        await self.request("PUT", "/fapi/v1/listenKey", weight=1)
