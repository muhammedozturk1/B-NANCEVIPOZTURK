"""
FAZ 5 - DASHBOARD API

Bu servis, ana bot (worker) servisinden BAĞIMSIZ çalışır ama AYNI veritabanını
okur. Botun API key'lerine ihtiyacı yok - mum verisi ve anlık fiyat için
Binance'in HERKESE AÇIK (public) uç noktalarını kullanır.
"""
import os
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func

from backend import config
from backend.db.database import get_session
from backend.db.models import Trade, TradeStatus, BotCapital
from backend.exchange.binance_client import BinanceClient
from backend.risk import kill_switch as kill_switch_module
from backend.db import repository

app = FastAPI(title="Crypto Bot Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PUBLIC_BASE_URL = "https://testnet.binancefuture.com" if config.USE_TESTNET else "https://fapi.binance.com"


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _public_ticker_price(binance_symbol: str):
    try:
        r = requests.get(f"{PUBLIC_BASE_URL}/fapi/v1/ticker/price",
                          params={"symbol": binance_symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def _status_value(status):
    return status.value if hasattr(status, "value") else status


def _serialize_trade(t: Trade):
    return {
        "id": t.id,
        "engine": t.engine,
        "symbol": t.symbol,
        "side": t.side,
        "leverage": t.leverage,
        "entry_price": t.entry_price,
        "stop_loss": t.stop_loss,
        "take_profit_1": t.take_profit_1,
        "take_profit_2": t.take_profit_2,
        "exit_price": t.exit_price,
        "pnl_usd": t.pnl_usd,
        "status": _status_value(t.status),
        "moved_to_breakeven": t.moved_to_breakeven,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


@app.get("/api/summary")
def summary():
    session = get_session()
    try:
        capital = session.query(BotCapital).first()
        current_balance = capital.balance if capital else config.STARTING_BALANCE

        open_trades = session.query(Trade).filter(Trade.status == TradeStatus.OPEN).all()
        closed_trades = session.query(Trade).filter(Trade.status != TradeStatus.OPEN).all()

        open_pnl = 0.0
        for t in open_trades:
            price = _public_ticker_price(t.symbol.replace("/", ""))
            if price:
                direction = 1 if t.side == "buy" else -1
                open_pnl += (price - t.entry_price) * t.amount * direction

        closed_pnl = sum(t.pnl_usd or 0 for t in closed_trades)

        return {
            "starting_balance": config.STARTING_BALANCE,
            "current_balance": round(current_balance, 2),
            "open_pnl": round(open_pnl, 2),
            "closed_pnl": round(closed_pnl, 2),
            "open_positions_count": len(open_trades),
        }
    finally:
        session.close()


@app.get("/api/trades")
def trades(status: str = "all"):
    session = get_session()
    try:
        query = session.query(Trade)
        if status == "open":
            query = query.filter(Trade.status == TradeStatus.OPEN)
        elif status == "closed":
            query = query.filter(Trade.status != TradeStatus.OPEN)
        rows = query.order_by(Trade.opened_at.desc()).limit(300).all()
        return [_serialize_trade(t) for t in rows]
    finally:
        session.close()


@app.get("/api/exit-breakdown")
def exit_breakdown():
    """TP1/TP2/BE/Stop dağılımı, motor bazlı - dashboard'un istediği özellik."""
    session = get_session()
    try:
        rows = session.query(Trade.engine, Trade.status, func.count(Trade.id)).filter(
            Trade.status != TradeStatus.OPEN
        ).group_by(Trade.engine, Trade.status).all()

        result = {}
        for engine, status, count in rows:
            result.setdefault(engine, {})[_status_value(status)] = count
        return result
    finally:
        session.close()


@app.get("/api/cumulative")
def cumulative(period: str = "daily"):
    """Günlük/haftalık/aylık kümülatif PnL - dashboard'un istediği özellik."""
    session = get_session()
    try:
        closed = session.query(Trade).filter(
            Trade.status != TradeStatus.OPEN, Trade.closed_at.isnot(None)
        ).order_by(Trade.closed_at.asc()).all()
    finally:
        session.close()

    if not closed:
        return []

    fmt = {"daily": "%Y-%m-%d", "weekly": "%Y-W%W", "monthly": "%Y-%m"}.get(period, "%Y-%m-%d")

    buckets = {}
    for t in closed:
        key = t.closed_at.strftime(fmt)
        buckets[key] = buckets.get(key, 0) + (t.pnl_usd or 0)

    cumulative_value = 0
    result = []
    for key in sorted(buckets.keys()):
        cumulative_value += buckets[key]
        result.append({"period": key, "pnl": round(buckets[key], 2), "cumulative": round(cumulative_value, 2)})
    return result


@app.get("/api/candles")
def candles(symbol: str = "BTC/USDT", timeframe: str = "15m", limit: int = 200):
    """Mum verisi - Binance'in herkese açık uç noktasından, API key gerekmez."""
    binance_symbol = symbol.replace("/", "")
    try:
        r = requests.get(f"{PUBLIC_BASE_URL}/fapi/v1/klines", params={
            "symbol": binance_symbol, "interval": timeframe, "limit": limit
        }, timeout=10)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        return {"error": str(e)}

    return [
        {"time": int(c[0] / 1000), "open": float(c[1]), "high": float(c[2]),
         "low": float(c[3]), "close": float(c[4])}
        for c in raw
    ]


@app.get("/api/symbols")
def symbols():
    session = get_session()
    try:
        rows = session.query(Trade.symbol).distinct().all()
        syms = sorted({r[0] for r in rows})
        if "BTC/USDT" not in syms:
            syms.insert(0, "BTC/USDT")
        return syms
    finally:
        session.close()


@app.post("/api/trades/{trade_id}/close")
def close_trade_manually(trade_id: int):
    """Dashboard'dan manuel pozisyon kapatma. Hem borsadaki pozisyonu hem
    ilişkili SL/TP algo emirlerini kapatır, veritabanını ve sanal kasayı günceller."""
    session = get_session()
    try:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            raise HTTPException(status_code=404, detail="İşlem bulunamadı")
        if trade.status != TradeStatus.OPEN:
            raise HTTPException(status_code=400, detail="Bu işlem zaten kapalı")

        trade_data = {"symbol": trade.symbol, "side": trade.side, "amount": trade.amount,
                      "entry_price": trade.entry_price, "engine": trade.engine}
    finally:
        session.close()

    client = BinanceClient()
    try:
        client.cancel_all_algo_orders(trade_data["symbol"])
    except Exception as e:
        pass  # emir zaten yoksa/iptal olmuşsa önemli değil

    try:
        client.close_position(trade_data["symbol"], trade_data["side"], trade_data["amount"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pozisyon kapatılamadı: {e}")

    exit_price = _public_ticker_price(trade_data["symbol"].replace("/", "")) or trade_data["entry_price"]
    direction = 1 if trade_data["side"] == "buy" else -1
    pnl_usd = (exit_price - trade_data["entry_price"]) * trade_data["amount"] * direction

    repository.close_trade(trade_id, exit_price, pnl_usd, TradeStatus.CLOSED_MANUAL)
    new_balance = repository.update_virtual_balance(pnl_usd)
    kill_switch_module.record_realized_pnl(pnl_usd, new_balance - pnl_usd)
    kill_switch_module.record_engine_trade_result(trade_data["engine"], is_win=(pnl_usd > 0))

    return {"success": True, "exit_price": exit_price, "pnl_usd": round(pnl_usd, 2), "new_balance": round(new_balance, 2)}
