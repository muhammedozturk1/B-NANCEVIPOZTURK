"""Testler internet ve API key OLMADAN çalışır: sahte piyasa + geçici SQLite."""
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="botdb_"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    from backend.db.database import Base, engine, init_db
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)
