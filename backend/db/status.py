"""
Bot durum kaydı: worker "yaşıyorum" ve "son döngü şöyle geçti" bilgisini veritabanına yazar,
dashboard (ayrı servis) bunu okuyup gerçek durumu gösterir.

Eskiden dashboard'daki "Bot Çalışıyor" yazısı SABİTTİ: worker çökse bile yeşil görünüyordu.

Bu modüldeki fonksiyonlar ASLA exception fırlatmaz - durum kaydı yazılamadı diye
işlem döngüsü bozulmasın.
"""
import json
import logging
from datetime import datetime

from backend.db.database import get_session
from backend.db.models import BotStatus

logger = logging.getLogger("bot_status")

WORKER = "worker"


def _upsert(component: str, **fields):
    session = get_session()
    try:
        row = session.query(BotStatus).filter(BotStatus.component == component).first()
        if row is None:
            row = BotStatus(component=component)
            session.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        session.commit()
    except Exception as e:  # pragma: no cover - sadece log
        session.rollback()
        logger.warning(f"Durum kaydı yazılamadı ({component}): {e}")
    finally:
        session.close()


def beat():
    """Worker canlılık sinyali."""
    _upsert(WORKER, last_ok_at=datetime.utcnow())


def mark_ok(component: str, info: dict = None):
    fields = {"last_ok_at": datetime.utcnow()}
    if info is not None:
        fields["info_json"] = json.dumps(info, ensure_ascii=False, default=str)
    _upsert(component, **fields)
    _upsert(WORKER, last_ok_at=datetime.utcnow())


def mark_error(component: str, error):
    _upsert(component, last_error_at=datetime.utcnow(), last_error=str(error)[:2000])


def read_all() -> dict:
    session = get_session()
    try:
        out = {}
        for row in session.query(BotStatus).all():
            try:
                info = json.loads(row.info_json) if row.info_json else None
            except ValueError:
                info = None
            out[row.component] = {
                "last_ok_at": row.last_ok_at,
                "last_error_at": row.last_error_at,
                "last_error": row.last_error,
                "info": info,
            }
        return out
    finally:
        session.close()
