"""Telegram bildirimleri. Kuyruk tabanlı; Telegram hatası trading'i asla durdurmaz."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from backend.smc.database.repo import Repo

log = logging.getLogger(__name__)

ICONS = {
    "NEW_LONG": "🟢", "NEW_SHORT": "🔴", "TP1": "🎯", "TP2": "🎯", "TP3": "🏁", "BREAKEVEN": "🛡", "SL": "⛔",
    "DAILY_LOSS": "📉", "CONSECUTIVE_LOSSES": "🧊", "MAX_RISK": "⚠️", "SPREAD": "↔️", "LIQ_BUFFER": "⚠️",
    "STARTUP": "🚀", "RESTART": "🔄", "TRIAL_START": "⏱", "TRIAL_EXPIRED": "⌛", "KILL_SWITCH": "🛑",
    "WS_DISCONNECT": "🔌", "WS_RECONNECT": "🔗", "API_ERROR": "❗", "ORDER_REJECTED": "🚫", "SL_FAILURE": "🚨",
    "TP_FAILURE": "⚠️", "RECONCILIATION": "🧮", "SAFE_MODE": "🟠", "CLOSED": "✅",
}


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, repo: "Repo | None" = None, env_label: str = "TESTNET",
                 client: httpx.AsyncClient | None = None):
        self.token = token
        self.chat_id = chat_id
        self.repo = repo
        self.env_label = env_label
        self._client = client or httpx.AsyncClient(timeout=10)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=500)
        self.enabled = bool(token and chat_id)

    def notify(self, kind: str, text: str) -> None:
        msg = f"{ICONS.get(kind, 'ℹ️')} [{self.env_label}] {text}"
        try:
            self._queue.put_nowait((kind, msg))
        except asyncio.QueueFull:
            log.warning("telegram kuyruğu dolu, mesaj atlandı: %s", kind)

    async def run(self) -> None:
        while True:
            kind, msg = await self._queue.get()
            sent, err = await self._send(msg)
            if self.repo is not None:
                try:
                    self.repo.telegram_log(kind, msg, sent, err)
                except Exception:
                    log.exception("telegram_events yazılamadı")
            await asyncio.sleep(1.05)  # Telegram ~1 msg/sn/chat

    async def _send(self, msg: str) -> tuple[bool, str | None]:
        if not self.enabled:
            log.info("telegram (devre dışı): %s", msg)
            return False, "disabled"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for attempt in range(3):
            try:
                r = await self._client.post(url, json={"chat_id": self.chat_id, "text": msg,
                                                       "disable_web_page_preview": True})
                if r.status_code == 200:
                    return True, None
                if r.status_code == 429:
                    retry = r.json().get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(float(retry))
                    continue
                return False, f"{r.status_code}: {r.text[:200]}"
            except Exception as exc:
                err = repr(exc)
                await asyncio.sleep(2 ** attempt)
        return False, locals().get("err", "failed")
