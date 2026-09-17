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


def get_session():
    return SessionLocal()
