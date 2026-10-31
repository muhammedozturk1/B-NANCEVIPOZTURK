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
                 max_backoff: float = 60.0, recv_timeout: float = 90.0):
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
        while not self._stop.is_set():
            try:
                await self._set(WSState.RECONNECTING)
                url = await self.url_factory()
                async with websockets.connect(url, ping_interval=None, max_size=2 ** 23, open_timeout=15) as ws:
                    self._ws = ws
                    self._restart.clear()
                    await self._set(WSState.RESUBSCRIBING)  # combined stream URL ile abonelik yapılır
                    await self._set(WSState.RECONCILING)
                    if self.on_connected:
                        await self.on_connected()
                    await self._set(WSState.CONNECTED)
                    attempt = 0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
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


def market_stream_url(base: str, symbols: list[str], intervals: tuple[str, ...] = ("3m", "5m", "15m")) -> str:
    streams = [f"{s.lower()}@kline_{iv}" for s in symbols for iv in intervals]
    streams.append("!markPrice@arr@1s")
    return f"{base.rstrip('/')}/stream?streams={'/'.join(streams)}"
