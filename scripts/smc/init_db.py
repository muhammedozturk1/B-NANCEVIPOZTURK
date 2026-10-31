"""Veritabanı tablolarını oluşturur (idempotent). Render pre-deploy komutu olarak çalışır.

    python -m scripts.smc.init_db
"""
from backend.smc.config import get_settings
from backend.smc.database.db import Database

if __name__ == "__main__":
    db = Database(get_settings().database_url)
    db.create_all()
    print("DB hazır:", "OK" if db.ping() else "BAĞLANTI HATASI")
