"""
DASHBOARD SERVİSİ (crypto-bot-dashboard)

- "/"      : SMC Bot paneli (backend/smcbot). Bot motoru da bu servisin içinde çalışır.
- "/smc/"  : SMC Scalper paneli (backend/smc). SMC Scalper motoru worker servisinde çalışır.
- Tüm sayfa ve API'ler tek oturum açma ekranının arkasında (DASHBOARD_USERNAME / DASHBOARD_PASSWORD_HASH).
  /health şifresizdir (Render health check).

v4: Fonlama (funding) botu ve paneli kaldırıldı; yerine SMC Bot geldi.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.db import status as bot_status
from backend.db.database import init_db
from backend.smc.api.main import create_smc_app, install_auth
from backend.smc.config import Settings as SmcSettings
from backend.smc.database.db import Database as SmcDatabase
from backend.smcbot import web as smcbot_web

_smc_settings = SmcSettings()
_smc_db = SmcDatabase(_smc_settings.database_url)


@asynccontextmanager
async def _lifespan(_app):
    # Dashboard worker'dan önce açılırsa (veya yeni tablolar henüz yoksa) hata vermesin.
    init_db()
    _smc_db.create_all()  # SMC Scalper tabloları (smc_ önekli)
    await smcbot_web.start()  # SMC Bot motoru (smcbot_ tabloları)
    try:
        yield
    finally:
        await smcbot_web.stop()


app = FastAPI(title="Crypto Bot Dashboard", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Tüm panel tek oturum açma ekranının arkasında. Şifre: DASHBOARD_PASSWORD_HASH
install_auth(app, _smc_settings, _smc_db)
# Şartname v2.1 SMC Scalper paneli: /smc/
app.mount("/smc", create_smc_app(_smc_settings, _smc_db))
# SMC Bot paneli API'si: /api/...
app.include_router(smcbot_web.router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _iso(dt):
    """DB'deki zamanlar UTC (saat dilimi bilgisi olmadan); 'Z' eklenmezse tarayıcı yerel saat sanar."""
    return dt.isoformat() + "Z" if dt else None


@app.get("/health")
def health():
    """Render health check (şifresiz). Sadece canlılık bilgisi verir, işlem verisi vermez."""
    from sqlalchemy import text
    from backend.db.database import engine
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"db": db_ok, "status": "ok" if db_ok else "down"}


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    """Worker (SMC Scalper + yönlü motorlar) gerçekten çalışıyor mu? Son heartbeat + bileşen bazlı durum."""
    now = datetime.utcnow()
    try:
        rows = bot_status.read_all()
    except Exception as e:
        return {"alive": False, "state": "unknown", "message": f"Durum okunamadı: {e}", "components": {}}

    worker = rows.get(bot_status.WORKER)
    last = worker["last_ok_at"] if worker else None
    age_min = (now - last).total_seconds() / 60 if last else None
    if last is None:
        state, msg = "unknown", "Worker henüz durum bildirmedi (güncel sürüm deploy edildi mi?)"
    elif age_min <= 15:
        state, msg = "alive", "Bot çalışıyor"
    else:
        state, msg = "dead", f"Bot {age_min:.0f} dakikadır sessiz - worker loglarını kontrol et"

    components = {}
    for name, r in rows.items():
        if name == bot_status.WORKER:
            continue
        err_recent = r["last_error_at"] and (not r["last_ok_at"] or r["last_error_at"] > r["last_ok_at"])
        components[name] = {
            "last_ok_at": _iso(r["last_ok_at"]),
            "last_error_at": _iso(r["last_error_at"]),
            "last_error": r["last_error"] if err_recent else None,
            "info": r["info"],
        }
    return {"alive": state == "alive", "state": state, "message": msg,
            "last_heartbeat": _iso(last), "components": components}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
