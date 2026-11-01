from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from backend.smc.config import Settings
from backend.smc.core.types import Candle
from backend.smc.database.db import Database
from backend.smc.database.repo import Repo
from backend.smc.market_data.universe import SymbolFilters
from backend.smc.notifications.telegram import TelegramNotifier


# Mevcut botla ortak (öneksiz) env değişkenleri. Ortamdaki değerler (ör. tests/conftest.py'nin
# DATABASE_URL'i) test parametrelerini ezmesin diye bunlar alias adlarıyla verilir.
_SHARED = {"binance_api_key": "BINANCE_API_KEY", "binance_api_secret": "BINANCE_API_SECRET",
           "database_url": "DATABASE_URL", "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
           "telegram_chat_id": "TELEGRAM_CHAT_ID", "dashboard_username": "DASHBOARD_USERNAME",
           "dashboard_password_hash": "DASHBOARD_PASSWORD_HASH", "session_secret": "SESSION_SECRET",
           "session_https_only": "SESSION_HTTPS_ONLY"}


def make_settings(**kw) -> Settings:
    """Varsayılan account_scope=exclusive: şartnamedeki hesap genelinde davranış.
    Ortak hesap (shared) davranışı ayrı testlerde açıkça istenir."""
    base = dict(database_url="sqlite:///:memory:", telegram_bot_token="", telegram_chat_id="",
                session_https_only=False, account_scope="exclusive", profile="spec")
    base.update(kw)
    return Settings(_env_file=None, **{_SHARED.get(k, k): v for k, v in base.items()})


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def zero_fee_settings() -> Settings:
    return make_settings(taker_fee=0.0, maker_fee=0.0, slippage_pct=0.0)


@pytest.fixture
def filters() -> SymbolFilters:
    return SymbolFilters("TESTUSDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10000"),
                         Decimal("0.001"), Decimal("10000"), 5.0, 20, 0.004, 0.0)


@pytest.fixture
def db() -> Database:
    d = Database("sqlite:///:memory:")
    d.create_all()
    return d


@pytest.fixture
def repo(db) -> Repo:
    return Repo(db)


class RecordingNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__("", "")
        self.messages: list[tuple[str, str]] = []

    def notify(self, kind: str, text: str) -> None:
        self.messages.append((kind, text))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.messages]


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def candle(i: int, o: float, h: float, l: float, c: float, v: float = 100.0, buy: float | None = None,
           tf_ms: int = 300_000, t0: int = 1_700_000_000_000) -> Candle:
    return Candle(t0 + i * tf_ms, o, h, l, c, v, t0 + (i + 1) * tf_ms - 1, v / 2 if buy is None else buy)


def from_closes(closes: list[float], spread: float = 0.2, tf_ms: int = 300_000, t0: int = 1_700_000_000_000) -> list[Candle]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(candle(i, o, max(o, c) + spread, min(o, c) - spread, c, tf_ms=tf_ms, t0=t0))
        prev = c
    return out


def run(coro):
    return asyncio.run(coro)
