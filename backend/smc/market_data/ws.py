"""Yeniden bağlanan WebSocket istemcisi.

Durum akışı: DISCONNECTED → (exponential backoff) → RECONNECT → RESUBSCRIBE →
RECONCILIATION (on_connected callback) → CONNECTED.
on_connected callback'i (eksik mum backfill + reconciliation) başarıyla bitmeden
`ready` True olmaz; worker ready olmayan bağlantıda yeni işlem açmaz.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from enum import Enum
from typing import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)


class WSState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    RESUBSCRIBING = "RESUBSCRIBING"
    RECONCILING = "RECONCILING"
    CONNECTED = "CONNECTED"


MessageHandler = Callable[[dict], Awaitable[None]]
StateHandler = Callable[[str, WSState], Awaitable[None]]


class ReconnectingWebSocket:
    def __init__(self, name: str, url_factory: Callable[[], Awaitable[str]], on_message: MessageHandler, *,
                 on_state: StateHandler | None = None, on_connected: Callable[[], Awaitable[None]] | None = None,
                 max_backoff: float = 60.0, recv_timeout: float | None = 90.0, stable_after: float = 10.0,
                 max_queue: int | None = 4096):
        """recv_timeout: bu kadar saniye mesaj gelmezse bağlantı ölü sayılır. Sürekli veri akan
        market stream için uygundur; hesap hareketsizken sessiz kalan user stream için None verilir
        (ölü bağlantı tespiti WebSocket ping/pong ile yapılır)."""
        self.name = name
        self.url_factory = url_factory
        self.on_message = on_message
        self.on_state = on_state
        self.on_connected = on_connected
        self.max_backoff = max_backoff
        self.recv_timeout = recv_timeout
        self.state = WSState.DISCONNECTED
        self._stop = asyncio.Event()
        self._restart = asyncio.Event()
        self._ws = None
        self.reconnects = 0
        # Kalıcı olamayan ardışık denemeler (URL fallback için). Sunucu bağlantıyı kabul edip hemen
        # kapatabilir; bu yüzden bağlantı ancak `stable_after` sn ayakta kalırsa veya mesaj gelirse başarılı sayılır.
        self.consecutive_failures = 0
        self.stable_after = stable_after
        # Gelen mesaj tamponu. Küçük olursa (websockets varsayılanı 16) yoğun market stream'de okuma
        # birkaç saniye gecikince soket okunmaz hale gelir ve Binance bağlantıyı "yavaş istemci" diye keser.
        self.max_queue = max_queue
        self.last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == WSState.CONNECTED

    async def _set(self, state: WSState) -> None:
        if state != self.state:
            self.state = state
            log.info("ws[%s] -> %s", self.name, state.value)
            if self.on_state:
                try:
                    await self.on_state(self.name, state)
                except Exception:  # pragma: no cover
                    log.exception("ws state handler failed")

    def restart(self) -> None:
        """Abonelik listesi değiştiğinde (universe yenileme) bağlantıyı yeniden kur."""
        self._restart.set()
        if self._ws is not None:
            asyncio.create_task(self._ws.close())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        attempt = 0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            stable = False
            try:
                await self._set(WSState.RECONNECTING)
                url = await self.url_factory()
                async with websockets.connect(url, ping_interval=60, ping_timeout=30, max_size=2 ** 23,
                                              max_queue=self.max_queue, open_timeout=15) as ws:
                    self._ws = ws
                    self._restart.clear()
                    await self._set(WSState.RESUBSCRIBING)  # combined stream URL ile abonelik yapılır
                    await self._set(WSState.RECONCILING)
                    if self.on_connected:
                        try:
                            await self.on_connected()
                        except Exception as exc:  # reconciliation/backfill hatası bağlantıyı düşürmez
                            log.exception("ws[%s] on_connected hatası (bağlantı korunuyor)", self.name)
                            self.last_error = f"on_connected: {exc!r}"
                    await self._set(WSState.CONNECTED)
                    connected_at = loop.time()
                    stable = False
                    while not self._stop.is_set():
                        timeout = self.recv_timeout
                        if not stable:  # kararlılık süresi dolunca başarılı say
                            left = self.stable_after - (loop.time() - connected_at)
                            timeout = left if timeout is None else min(timeout, left)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=max(timeout, 0.01)) \
                                if timeout is not None else await ws.recv()
                        except asyncio.TimeoutError:
                            if not stable and loop.time() - connected_at >= self.stable_after:
                                stable = True
                                attempt = 0
                                self.consecutive_failures = 0
                                continue
                            raise
                        if not stable:
                            stable = True
                            attempt = 0
                            self.consecutive_failures = 0
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        try:
                            await self.on_message(msg)
                        except Exception:
                            log.exception("ws[%s] message handler failed", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ws[%s] bağlantı hatası: %r", self.name, exc)
                self.last_error = repr(exc)[:300]
                if not stable:
                    self.consecutive_failures += 1
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            await self._set(WSState.DISCONNECTED)
            if self._restart.is_set():
                self._restart.clear()
                continue
            self.reconnects += 1
            delay = min(self.max_backoff, 2 ** attempt) + random.uniform(0, 1)
            attempt += 1
            await asyncio.sleep(delay)
        await self._set(WSState.DISCONNECTED)


def market_stream_url(base: str, symbols: list[str], intervals: tuple[str, ...] = ("3m", "5m", "15m"),
                      variant: int = 0) -> str:
    """variant 0: klasik /stream ; variant 1: Binance'in 2026 yeni yolu /market/stream (kline, markPrice)."""
    streams = [f"{s.lower()}@kline_{iv}" for s in symbols for iv in intervals]
    streams.append("!markPrice@arr@1s")
    path = "/stream" if variant % 2 == 0 else "/market/stream"
    return f"{base.rstrip('/')}{path}?streams={'/'.join(streams)}"


USER_EVENTS = "ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE"


def user_stream_url(base: str, listen_key: str, variant: int = 0) -> str:
    """Hesap (user data) stream adresi. Binance 2026'da yeni '/private' yolunu getirdi ve eski
    yolları kaldırma sürecine girdi; hangi adresin çalıştığı ortama göre değişebildiği için
    bağlanamazsa sıradaki biçim denenir."""
    b = base.rstrip("/")
    variants = [
        f"{b}/private/ws?listenKey={listen_key}&events={USER_EVENTS}",
        f"{b}/ws/{listen_key}",
        f"{b}/private/ws?listenKey={listen_key}",
    ]
    return variants[variant % len(variants)]


def pick_variant(preferred: int, failures: int, count: int) -> int:
    """Son başarılı biçimden başla; her başarısız denemede bir sonrakine geç."""
    return (preferred + failures) % count
