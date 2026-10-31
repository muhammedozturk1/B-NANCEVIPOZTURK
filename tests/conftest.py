"""Testler internet ve API key OLMADAN çalışır: sahte piyasa + geçici SQLite."""
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="botdb_"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dashboard artık şifreli: testler için bilinen bir şifre
from backend.smc.api.auth import hash_password  # noqa: E402

TEST_DASHBOARD_PASSWORD = "test-password-123"
os.environ["DASHBOARD_USERNAME"] = "admin"
os.environ["DASHBOARD_PASSWORD_HASH"] = hash_password(TEST_DASHBOARD_PASSWORD, iterations=1000)
os.environ["SESSION_HTTPS_ONLY"] = "false"
os.environ["SESSION_SECRET"] = "test-secret"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    from backend.db.database import Base, engine, init_db
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def login(client):
    """Dashboard test istemcisinde oturum açar."""
    r = client.post("/login", data={"username": "admin", "password": TEST_DASHBOARD_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client
