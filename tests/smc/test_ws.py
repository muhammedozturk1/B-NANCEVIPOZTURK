"""WebSocket: user stream sessizken kopmamalı; adres reddedilirse sıradaki biçim denenmeli."""
import asyncio

import websockets

from backend.smc.market_data.ws import (ReconnectingWebSocket, WSState, market_stream_url, pick_variant,
                                        user_stream_url)


def test_url_variants():
    assert user_stream_url("wss://x", "K", 0) == "wss://x/private/ws?listenKey=K&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE"
    assert user_stream_url("wss://x/", "K", 1) == "wss://x/ws/K"
    assert user_stream_url("wss://x", "K", 3) == user_stream_url("wss://x", "K", 0)
    assert market_stream_url("wss://x", ["BTCUSDT"], ("3m",), 0).startswith("wss://x/stream?streams=btcusdt@kline_3m")
    assert market_stream_url("wss://x", ["BTCUSDT"], ("3m",), 1).startswith("wss://x/market/stream?streams=")
    assert [pick_variant(1, f, 3) for f in range(4)] == [1, 2, 0, 1]


def test_silent_user_stream_stays_connected_and_url_fallback():
    async def go():
        accepted = []

        async def handler(conn):
            path = conn.request.path
            if path.startswith("/private"):  # bu sunucuda yeni yol yok -> reddet
                await conn.close(code=1008)
                return
            accepted.append(path)
            await asyncio.sleep(3)  # hiç mesaj göndermeden bekle (hesap hareketsiz)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            holder = {}

            async def url():
                v = pick_variant(0, holder["ws"].consecutive_failures, 3)
                return user_stream_url(f"ws://127.0.0.1:{port}", "KEY", v)

            async def noop(_):
                pass

            ws = ReconnectingWebSocket("user", url, noop, recv_timeout=None, max_backoff=0.05, stable_after=0.5)
            holder["ws"] = ws
            task = asyncio.create_task(ws.run())
            for _ in range(200):
                await asyncio.sleep(0.05)
                if accepted and ws.state == WSState.CONNECTED:
                    break
            assert ws.state == WSState.CONNECTED and accepted == ["/ws/KEY"]
            await asyncio.sleep(1.5)  # sessizlik bağlantıyı düşürmemeli
            assert ws.state == WSState.CONNECTED and ws.reconnects >= 1
            await ws.stop()
            task.cancel()
    asyncio.run(go())


def test_on_connected_error_does_not_drop_connection():
    async def go():
        async def handler(conn):
            await asyncio.sleep(2)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            async def url():
                return f"ws://127.0.0.1:{port}/ws/K"

            async def boom():
                raise RuntimeError("reconcile hatası")

            async def noop(_):
                pass
            ws = ReconnectingWebSocket("user", url, noop, on_connected=boom, recv_timeout=None, stable_after=0.2)
            task = asyncio.create_task(ws.run())
            for _ in range(60):
                await asyncio.sleep(0.05)
                if ws.state == WSState.CONNECTED:
                    break
            assert ws.state == WSState.CONNECTED and "reconcile" in ws.last_error
            await ws.stop()
            task.cancel()
    asyncio.run(go())
