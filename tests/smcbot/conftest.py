import pytest


@pytest.fixture(autouse=True)
def clean_smcbot_tables():
    """Her testten önce SMC Bot tablolarını boşalt (ortak kök conftest sadece eski bot tablolarını siler)."""
    from backend.smcbot import db
    db.init()
    db.wipe_all()
    yield
