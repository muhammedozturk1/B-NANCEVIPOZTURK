"""SMC Bot paneli API'si. Dashboard uygulamasına (backend/dashboard/api.py) eklenir.

- Giriş: mevcut panel girişi (install_auth middleware'i tüm /api yollarını korur).
- Durum değiştiren her istek X-CSRF-Token başlığı ister (SMC Scalper paneliyle aynı yöntem).
- Bot motoru dashboard sürecinde çalışır: start()/stop() dashboard'un lifespan'inden çağrılır.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from . import config, db, telegram
from .bot import Bot

log = logging.getLogger("smcbot")
router = APIRouter()
_bot: Bot | None = None


async def start() -> None:
    global _bot
    if not config.ENABLED:
        log.info("SMC Bot kapalı (SMCBOT_ENABLED=false)")
        return
    try:
        db.init()
        _bot = Bot()
        await _bot.start()
    except Exception:  # bot açılamazsa panelin geri kalanı çalışmaya devam etsin
        log.exception("SMC Bot başlatılamadı")
        if _bot is not None:
            await _bot.stop()
        _bot = None


async def stop() -> None:
    global _bot
    if _bot is not None:
        await _bot.stop()
        _bot = None


def B() -> Bot:
    if _bot is None:
        raise HTTPException(503, "SMC Bot çalışmıyor (SMCBOT_ENABLED=false veya başlatma hatası; dashboard loglarına bak)")
    return _bot


def csrf(request: Request) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "Oturum doğrulaması başarısız (sayfayı yenile)")


async def body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def run(coro_or_value):
    """Bot eylemlerindeki ValueError'ları kullanıcıya gösterilecek 400 hatasına çevirir."""
    try:
        if hasattr(coro_or_value, "__await__"):
            await coro_or_value
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


# ---------------------------------------------------------------- oturum
@router.get("/api/session")
async def session_info(request: Request):
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf": request.session["csrf"], "user": request.session.get("user")}


# ---------------------------------------------------------------- veri
@router.get("/api/state")
async def state():
    st = await B().state()
    st["worker"] = _worker_status()
    return st


def _worker_status() -> dict:
    """SMC Scalper'ın çalıştığı worker servisinin durumu (mevcut botun bot_status tablosundan)."""
    try:
        from datetime import datetime
        from backend.db import status as bot_status
        rows = bot_status.read_all()
        w = rows.get(bot_status.WORKER)
        last = w["last_ok_at"] if w else None
        age = (datetime.utcnow() - last).total_seconds() / 60 if last else None
        smc = rows.get("smc") or {}
        err = smc.get("last_error") if smc.get("last_error_at") and (
            not smc.get("last_ok_at") or smc["last_error_at"] > smc["last_ok_at"]) else None
        return {"alive": age is not None and age <= 15, "age_min": age, "smc_error": err}
    except Exception as e:  # noqa: BLE001
        return {"alive": None, "age_min": None, "smc_error": f"okunamadı: {e}"}


@router.get("/api/trades")
async def trades(status: str = "closed", limit: int = 100, offset: int = 0):
    st = tuple(x for x in status.split(",") if x) or None
    rows = db.list_trades(B().mode, st, limit=max(1, min(limit, 500)), offset=max(0, offset))
    for r in rows:
        r.pop("annotations", None)
    return {"trades": rows}


@router.get("/api/trade/{trade_id}")
async def trade(trade_id: int):
    t = db.get_trade(trade_id)
    if not t:
        raise HTTPException(404, "İşlem bulunamadı")
    t["mark"] = B().md.mark(t["symbol"])
    return t


@router.get("/api/candles")
async def candles(symbol: str, interval: str = "15m", start: int = 0, end: int = 0):
    if interval not in config.INTERVAL_MS:
        raise HTTPException(400, "Geçersiz aralık")
    b = B()
    end = end or b.now()
    start = start or end - 200 * config.INTERVAL_MS[interval]
    return {"candles": await b.md.chart_candles(symbol.upper(), interval, start, end)}


@router.get("/api/equity")
async def equity():
    return {"points": db.equity_series(B().mode)}


@router.get("/api/logs")
async def logs(after: int = 0):
    return {"logs": db.get_logs(after, 300)}


@router.get("/api/settings")
async def get_settings():
    return {"settings": B().settings, "defaults": config.DEFAULT_SETTINGS}


@router.post("/api/settings")
async def set_settings(request: Request):
    csrf(request)
    b = B()
    if not b.is_leader:
        raise HTTPException(409, "Bot şu an diğer sunucu kopyasında; birkaç saniye sonra tekrar dene.")
    data = await body(request)
    for k in ("mode", "running"):
        data.pop(k, None)  # bunlar ayrı uç noktalardan değişir
    s = b.save_settings(data)
    b.log("info", "Ayarlar güncellendi")
    return {"ok": True, "settings": s}


# ---------------------------------------------------------------- eylemler
@router.post("/api/bot/start")
async def start_bot(request: Request):
    csrf(request)
    return await run(B().set_running(True))


@router.post("/api/bot/pause")
async def pause_bot(request: Request):
    csrf(request)
    return await run(B().set_running(False))


@router.post("/api/bot/emergency")
async def emergency(request: Request):
    csrf(request)
    return await run(B().emergency())


@router.post("/api/trade/{trade_id}/close")
async def close_trade(trade_id: int, request: Request):
    csrf(request)
    return await run(B().close_trade(trade_id))


@router.post("/api/reset")
async def reset(request: Request):
    csrf(request)
    data = await body(request)
    if str(data.get("confirm", "")).strip().upper() != "SIFIRLA":
        raise HTTPException(400, "Onay için SIFIRLA yaz")
    return await run(B().reset())


@router.post("/api/mode")
async def mode(request: Request):
    csrf(request)
    data = await body(request)
    return await run(B().switch_mode(str(data.get("mode", "")), str(data.get("confirm", ""))))


@router.post("/api/risk/unlock")
async def unlock(request: Request):
    csrf(request)
    try:
        B().unlock_risk()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.post("/api/telegram/test")
async def tg_test(request: Request):
    csrf(request)
    sent, err = await telegram.send("✅ <b>SMC Bot</b> Telegram bağlantısı çalışıyor.")
    if not sent:
        raise HTTPException(400, err)
    return {"ok": True}


@router.post("/api/backtest")
async def backtest_start(request: Request):
    csrf(request)
    data = await body(request)
    try:
        B().start_backtest(int(data.get("days", 30)), int(data.get("symbols", 10)))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.get("/api/backtest")
async def backtest_result():
    return {"state": B().backtest_state, "last": db.last_backtest()}
