"""Dashboard katmanı.

- `install_auth(app, ...)`: Mevcut botun dashboard'u (funding paneli) DAHİL tüm panele tek bir
  oturum açma ekranı ekler: PBKDF2 şifre hash'i, imzalı session cookie (SameSite=strict),
  giriş denemesi sınırı ve state değiştiren isteklerde Origin kontrolü.
- `create_smc_app(...)`: SMC paneli (/smc altına mount edilir). Trading engine'den bağımsızdır:
  yalnızca veritabanını okur, KILL / RESUME komutlarını smc_bot_commands tablosuna yazar.
  Dashboard çökse bile worker ve Binance üzerindeki native SL/TP emirleri çalışmaya devam eder.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from starlette.middleware.sessions import SessionMiddleware

from backend.smc.api.auth import LoginRateLimiter, verify_password
from backend.smc.config import Settings, get_settings
from backend.smc.core.trial import trial_status
from backend.smc.core.types import ensure_utc, utc_now
from backend.smc.database.db import Database
from backend.smc.database.models import (BotCommand, Position, RiskEvent, Signal, SignalComponent, SystemEvent, Trade,
                                         TrialState, User)
from backend.smc.database.repo import Repo
from backend.smc.execution.state_machine import LIVE_STATES, PosState

log = logging.getLogger("smc.dashboard")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "dashboard" / "templates"))
PUBLIC_PATHS = {"/login", "/health", "/smc/health", "/favicon.ico"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # tarayıcılar cross-site isteklerde Origin gönderir; yoksa (curl vb.) cookie'siz zaten 401
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlparse(origin).netloc == host


def install_auth(app: FastAPI, s: Settings, database: Database) -> None:
    """Uygulamanın TÜM sayfa ve API'lerini oturum açmaya bağlar (PUBLIC_PATHS hariç)."""
    repo = Repo(database)
    limiter = LoginRateLimiter()
    secret = s.session_secret.get_secret_value()
    if not secret:
        secret = secrets.token_urlsafe(48)
        log.warning("SESSION_SECRET ayarlanmamış: geçici anahtar üretildi (restart'ta oturumlar düşer).")

    def password_configured() -> bool:
        return bool(s.dashboard_password_hash.get_secret_value())

    def seed_user() -> None:
        pw_hash = s.dashboard_password_hash.get_secret_value()
        if not pw_hash:
            return
        with database.session() as sess:
            u = sess.scalar(select(User).where(User.username == s.dashboard_username))
            if u is None:
                sess.add(User(username=s.dashboard_username, password_hash=pw_hash))
            elif u.password_hash != pw_hash:
                u.password_hash = pw_hash

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        if request.method in UNSAFE_METHODS and not _same_origin(request):
            return JSONResponse({"detail": "cross-origin istek reddedildi"}, status_code=403)
        if path in PUBLIC_PATHS:
            return await call_next(request)
        if not request.session.get("user"):
            if "/api/" in path or request.method != "GET":
                return JSONResponse({"detail": "auth required"}, status_code=401)
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        return await call_next(request)

    # add_middleware en dışa ekler: Session middleware auth_guard'dan ÖNCE çalışmalı
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict",
                       https_only=s.session_https_only, max_age=12 * 3600, session_cookie="bot_session")

    def safe_next(nxt: str | None) -> str:
        return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        err = None if password_configured() else \
            "DASHBOARD_PASSWORD_HASH ayarlanmamış. Render'da ekleyin (python -m scripts.smc.hash_password)."
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": err, "next": safe_next(next)})

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
        key = request.client.host if request.client else "?"
        ctx = {"next": safe_next(next)}
        if not limiter.allowed(key):
            return TEMPLATES.TemplateResponse(request, "login.html", {**ctx, "error": "Çok fazla deneme. 15 dk bekleyin."},
                                              status_code=429)
        seed_user()
        with database.session() as sess:
            u = sess.scalar(select(User).where(User.username == username))
            ok = u is not None and verify_password(password, u.password_hash)
            if ok:
                u.last_login_at = utc_now()
        if not ok:
            limiter.fail(key)
            repo.audit(username, "login_failed", ip=key)
            return TEMPLATES.TemplateResponse(request, "login.html", {**ctx, "error": "Hatalı kullanıcı adı veya şifre"},
                                              status_code=401)
        limiter.reset(key)
        request.session.clear()
        request.session["user"] = username
        request.session["csrf"] = secrets.token_urlsafe(32)
        repo.audit(username, "login", ip=key)
        return RedirectResponse(ctx["next"], status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)


def create_smc_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    """SMC paneli. install_auth uygulanmış bir uygulamaya `/smc` altında mount edilir."""
    s = settings or get_settings()
    database = db or Database(s.database_url)
    repo = Repo(database)
    http = httpx.AsyncClient(timeout=10)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await http.aclose()

    app = FastAPI(title="SMC Scalper", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def current_user(request: Request) -> str:
        user = request.session.get("user") if "session" in request.scope else None
        if not user:
            raise HTTPException(status_code=401, detail="auth required")
        return user

    def csrf_check(request: Request) -> None:
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(status_code=403, detail="csrf")

    def is_expired() -> bool:
        with database.session() as sess:
            row = sess.get(TrialState, 1)
            return bool(row and trial_status(row, utc_now()).expired)

    @app.get("/health")
    async def health():
        db_ok = database.ping()
        body: dict = {"db": db_ok}
        if db_ok:
            try:
                st = repo.state()
            except Exception:
                return JSONResponse({"db": True, "status": "not_initialized"}, status_code=200)
            hb = ensure_utc(st.last_heartbeat_at)
            age = (utc_now() - hb).total_seconds() if hb else None
            with database.session() as sess:
                row = sess.get(TrialState, 1)
                tstat = trial_status(row, utc_now()) if row else None
            body.update(
                state=st.state, reason=st.reason,
                worker={"alive": age is not None and age < s.heartbeat_stale_s, "heartbeat_age_s": age,
                        "last_heartbeat_at": hb.isoformat() if hb else None},
                binance_rest=st.rest_ok, websocket={"market": st.ws_market_connected, "user": st.ws_user_connected},
                clock_offset_ms=st.clock_offset_ms,
                trial={"remaining_hours": round(tstat.remaining_s / 3600, 2), "expired": tstat.expired,
                       "expires_at": tstat.expires_at.isoformat()} if tstat else None)
            body["status"] = "ok" if body["worker"]["alive"] else "degraded"
        else:
            body["status"] = "down"
        return JSONResponse(body, status_code=200 if db_ok else 503)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, user: str = Depends(current_user)):
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "csrf": request.session.get("csrf", ""), "user": user, "env": s.trading_env.upper(),
            "base": request.scope.get("root_path", "").rstrip("/")})

    @app.get("/api/summary")
    async def summary(user: str = Depends(current_user)):
        st = repo.state()
        now = utc_now()
        with database.session() as sess:
            trial = sess.get(TrialState, 1)
            tstat = trial_status(trial, now) if trial else None
            positions = list(sess.scalars(select(Position).where(
                Position.state.in_([x.value for x in LIVE_STATES | {PosState.ORDER_SUBMITTED}]))))
            trades = list(sess.scalars(select(Trade).order_by(desc(Trade.exit_time)).limit(100)))
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            all_trades = list(sess.scalars(select(Trade).where(Trade.exit_time >= month_start)))
            events = list(sess.scalars(select(SystemEvent).order_by(desc(SystemEvent.id)).limit(30)))
            risk_events = list(sess.scalars(select(RiskEvent).order_by(desc(RiskEvent.id)).limit(20)))
            signals = list(sess.scalars(select(Signal).order_by(desc(Signal.id)).limit(30)))
            cmds = list(sess.scalars(select(BotCommand).order_by(desc(BotCommand.id)).limit(5)))
            realized_total = sum(t.net_pnl for t in sess.scalars(select(Trade)))
        equity = st.equity or s.starting_equity
        open_risk_total = sum(p.open_risk for p in positions)
        hb = ensure_utc(st.last_heartbeat_at)
        expired = bool(tstat and tstat.expired)
        return {
            "env": s.trading_env.upper(), "state": st.state, "reason": st.reason, "read_only": expired,
            "heartbeat_age_s": (now - hb).total_seconds() if hb else None,
            "worker_alive": bool(hb and (now - hb).total_seconds() < s.heartbeat_stale_s),
            "ws": {"market": st.ws_market_connected, "user": st.ws_user_connected}, "rest_ok": st.rest_ok,
            "account": {
                "equity": equity, "starting_equity": s.starting_equity, "available_balance": st.available_balance,
                "margin_used": st.margin_used, "unrealized_pnl": st.unrealized_pnl, "realized_pnl": realized_total,
                "daily_pnl": sum(t.net_pnl for t in all_trades if ensure_utc(t.exit_time) >= day_start),
                "daily_equity_change": equity - (st.day_start_equity or equity),
                "monthly_pnl": sum(t.net_pnl for t in all_trades), "equity_mode": s.equity_mode,
                "trial_remaining_s": tstat.remaining_s if tstat else None,
                "trial_expires_at": tstat.expires_at.isoformat() if tstat else None,
            },
            "risk": {
                "risk_per_trade": s.risk_per_trade, "max_open_risk": s.max_total_open_risk,
                "daily_loss_limit": s.daily_loss_limit, "current_open_risk": open_risk_total,
                "profile": s.profile,
                "current_open_risk_pct": open_risk_total / equity if equity else 0,
                "margin_usage_pct": (st.margin_used or 0) / equity if equity else 0, "max_leverage": s.max_leverage,
                "consecutive_losses": st.consecutive_losses,
                "cooldown_until": ensure_utc(st.cooldown_until).isoformat() if st.cooldown_until else None,
                "day_start_equity": st.day_start_equity,
            },
            "positions": [_pos_dict(p) for p in positions],
            "trades": [_trade_dict(t) for t in trades],
            "signals": [{"id": x.id, "symbol": x.symbol, "side": x.side, "score": x.score, "accepted": x.accepted,
                         "reason": x.reject_reason, "time": x.created_at.isoformat() if x.created_at else None}
                        for x in signals],
            "events": [{"ts": e.ts.isoformat() if e.ts else None, "level": e.level, "kind": e.kind,
                        "message": e.message} for e in events],
            "risk_events": [{"ts": e.ts.isoformat() if e.ts else None, "kind": e.kind, "symbol": e.symbol,
                             "details": e.details} for e in risk_events],
            "commands": [{"command": c.command, "status": c.status, "result": c.result,
                          "created_at": c.created_at.isoformat() if c.created_at else None} for c in cmds],
        }

    @app.get("/api/position/{pos_id}/chart")
    async def chart(pos_id: int, user: str = Depends(current_user)):
        with database.session() as sess:
            p = sess.get(Position, pos_id)
            if p is None:
                raise HTTPException(404)
            sig = sess.get(Signal, p.signal_id) if p.signal_id else None
            comps = list(sess.scalars(select(SignalComponent).where(SignalComponent.signal_id == p.signal_id))) \
                if p.signal_id else []
            trade = sess.scalar(select(Trade).where(Trade.position_id == p.id))
        start = ensure_utc(p.opened_at) - timedelta(hours=4)
        end = ensure_utc(p.closed_at) + timedelta(hours=1) if p.closed_at else utc_now()
        try:
            r = await http.get(f"{s.binance_rest_url}/fapi/v1/klines", params={
                "symbol": p.symbol, "interval": "3m", "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000), "limit": 1000})
            r.raise_for_status()
            klines = [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                       "close": float(k[4])} for k in r.json()]
        except Exception as exc:
            klines, err = [], repr(exc)
        else:
            err = None
        return {"position": _pos_dict(p), "trade": _trade_dict(trade) if trade else None, "klines": klines,
                "snapshot": sig.snapshot if sig else {}, "error": err,
                "components": [{"name": c.name, "points": c.points, "max": c.max_points, "passed": c.passed,
                                "mandatory": c.mandatory} for c in comps]}

    @app.post("/api/kill")
    async def kill(request: Request, user: str = Depends(current_user)):
        csrf_check(request)
        if is_expired():
            raise HTTPException(409, "Trial bitti; dashboard salt-okunur")
        cid = repo.add_command("KILL", user)
        repo.audit(user, "kill_switch_requested", command_id=cid)
        return {"ok": True, "command_id": cid}

    @app.post("/api/resume")
    async def resume(request: Request, user: str = Depends(current_user)):
        csrf_check(request)
        if is_expired():
            raise HTTPException(409, "Trial bitti; dashboard salt-okunur")
        cid = repo.add_command("RESUME", user)
        repo.audit(user, "resume_requested", command_id=cid)
        return {"ok": True, "command_id": cid}

    return app


def _pos_dict(p: Position) -> dict:
    return {"id": p.id, "symbol": p.symbol, "side": p.side, "state": p.state, "entry": p.entry_price,
            "planned_entry": p.planned_entry, "qty": p.filled_qty, "remaining": p.remaining_qty,
            "notional": p.notional, "leverage": p.leverage, "sl": p.stop_price, "initial_sl": p.initial_stop,
            "tp1": p.tp1_price, "tp2": p.tp2_price, "tp3": p.tp3_price, "tp_plan": p.tp_plan,
            "risk_amount": p.risk_amount, "open_risk": p.open_risk, "liq_price": p.liq_price,
            "be_active": p.be_active, "score": p.signal_score, "exit_reason": p.exit_reason,
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None}


def _trade_dict(t: Trade) -> dict:
    dur = (ensure_utc(t.exit_time) - ensure_utc(t.entry_time)).total_seconds() if t.exit_time and t.entry_time else None
    return {"id": t.id, "position_id": t.position_id, "symbol": t.symbol, "side": t.side, "entry": t.entry_price,
            "exit": t.exit_price, "qty": t.quantity, "net_pnl": t.net_pnl, "gross_pnl": t.gross_pnl,
            "commission": t.commission, "funding": t.funding, "slippage": t.slippage, "r": t.realized_rr,
            "planned_rr": t.planned_rr, "duration_s": dur, "exit_reason": t.exit_reason, "score": t.signal_score,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None, "tp_hits": t.tp_hits}


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    """Bağımsız çalıştırma (yerel geliştirme / test): giriş + /smc paneli."""
    s = settings or get_settings()
    database = db or Database(s.database_url)
    database.create_all()
    app = FastAPI(title="SMC Scalper", docs_url=None, redoc_url=None, openapi_url=None)
    install_auth(app, s, database)
    app.mount("/smc", create_smc_app(s, database))

    @app.get("/")
    async def root():
        return RedirectResponse("/smc/", status_code=303)

    @app.get("/health")
    async def health():
        return JSONResponse({"db": database.ping()}, status_code=200 if database.ping() else 503)

    return app
