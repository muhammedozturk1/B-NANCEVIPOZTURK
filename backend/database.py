"""SQLAlchemy engine ve session yönetimi."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from backend import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db():
    """Tüm tabloları oluşturur. main.py başlarken bir kere çağrılır."""
    from backend.db import models  # noqa: F401 (modellerin kayıtlı olması için)
    Base.metadata.create_all(bind=engine)
    _migrate_add_missing_columns()


# create_all mevcut tabloya yeni sütun EKLEMEZ. Render'daki eski Postgres
# veritabanı bozulmasın diye v2 sütunlarını elle ekliyoruz.
_V2_COLUMNS = {
    "setup": "VARCHAR",
    "ai_probability": "FLOAT",
    "features_json": "TEXT",
    "fees_usd": "FLOAT",
}


def _migrate_add_missing_columns():
    from sqlalchemy import inspect, text
    existing = {c["name"] for c in inspect(engine).get_columns("trades")}
    with engine.begin() as conn:
        for name, sql_type in _V2_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}"))


def get_session():
    return SessionLocal()
