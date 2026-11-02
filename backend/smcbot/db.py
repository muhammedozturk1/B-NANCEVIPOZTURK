"""SMC Bot kalıcılık katmanı: mevcut botun veritabanı (Render Postgres; lokalde/testte SQLite).

Tüm tablolar `smcbot_` öneklidir; mevcut botun `trades` vb. tablolarıyla ve SMC Scalper'ın
`smc_` tablolarıyla çakışmaz.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from sqlalchemy import text

from . import config

_lock = threading.RLock()
_ready = False
_log_inserts = 0

JSON_COLS = ("reasons", "annotations", "meta")
TRADE_COLS = (
    "mode", "symbol", "side", "status", "score", "setup_time", "entry_time", "entry_price", "qty", "qty_open",
    "notional", "leverage", "margin", "risk_amount", "sl", "sl_initial", "tp1", "tp2", "tp1_hit", "exit_time",
    "exit_price", "pnl", "fees", "funding", "r_multiple", "exit_reason", "mfe", "mae", "reasons", "annotations", "meta",
)


def engine():
    from backend.db.database import engine as eng
    return eng


def _pg() -> bool:
    return engine().dialect.name == "postgresql"


def _schema() -> list[str]:
    pk = "BIGSERIAL PRIMARY KEY" if _pg() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    f = "DOUBLE PRECISION" if _pg() else "REAL"
    return [
        "CREATE TABLE IF NOT EXISTS smcbot_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        f"""CREATE TABLE IF NOT EXISTS smcbot_trades (
            id {pk}, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL,
            score {f}, setup_time BIGINT, entry_time BIGINT, entry_price {f}, qty {f}, qty_open {f},
            notional {f}, leverage INTEGER, margin {f}, risk_amount {f}, sl {f}, sl_initial {f}, tp1 {f}, tp2 {f},
            tp1_hit INTEGER DEFAULT 0, exit_time BIGINT, exit_price {f}, pnl {f} DEFAULT 0, fees {f} DEFAULT 0,
            funding {f} DEFAULT 0, r_multiple {f}, exit_reason TEXT, mfe {f} DEFAULT 0, mae {f} DEFAULT 0,
            reasons TEXT, annotations TEXT, meta TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_smcbot_trades_mode_status ON smcbot_trades(mode, status)",
        f"CREATE TABLE IF NOT EXISTS smcbot_equity (id {pk}, mode TEXT NOT NULL, ts BIGINT NOT NULL, balance {f} NOT NULL, equity {f} NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_smcbot_equity_mode_ts ON smcbot_equity(mode, ts)",
        f"CREATE TABLE IF NOT EXISTS smcbot_logs (id {pk}, ts BIGINT NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS smcbot_backtests (id {pk}, ts BIGINT NOT NULL, params TEXT, result TEXT)",
        "CREATE TABLE IF NOT EXISTS smcbot_lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL, until BIGINT NOT NULL)",
    ]


def init() -> None:
    global _ready
    with _lock:
        if _ready:
            return
        with engine().begin() as c:
            for stmt in _schema():
                c.execute(text(stmt))
        _ready = True


def _clean(v: Any) -> Any:
    """numpy sayıları (np.int64 vb.) veritabanı sürücülerine uygun Python tiplerine çevrilir."""
    if v is not None and type(v).__module__ == "numpy":
        return v.item()
    return v


def _exec(sql: str, params: dict | None = None):
    init()
    with _lock, engine().begin() as c:
        return c.execute(text(sql), {k: _clean(v) for k, v in (params or {}).items()})


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    init()
    with _lock, engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), {k: _clean(v) for k, v in (params or {}).items()}).mappings()]


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------- key/value ----------
def kv_get(key: str, default: Any = None) -> Any:
    rows = _rows("SELECT value FROM smcbot_kv WHERE key=:k", {"k": key})
    return json.loads(rows[0]["value"]) if rows else default


def kv_set(key: str, value: Any) -> None:
    _exec("INSERT INTO smcbot_kv(key,value) VALUES(:k,:v) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          {"k": key, "v": json.dumps(value, default=_clean)})


def kv_delete_prefix(prefix: str) -> None:
    _exec("DELETE FROM smcbot_kv WHERE key LIKE :p", {"p": prefix + "%"})


# ---------- settings ----------
def get_settings() -> dict:
    stored = kv_get("settings", {}) or {}
    merged = dict(config.DEFAULT_SETTINGS)
    merged.update({k: v for k, v in stored.items() if k in config.DEFAULT_SETTINGS})
    return merged


def save_settings(values: dict) -> dict:
    current = get_settings()
    for k, v in values.items():
        if k not in config.DEFAULT_SETTINGS:
            continue
        default = config.DEFAULT_SETTINGS[k]
        try:
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "evet", "on")
            elif isinstance(default, int) and not isinstance(default, bool):
                v = int(float(v))
            elif isinstance(default, float):
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        current[k] = v
    kv_set("settings", current)
    return current


# ---------- trades ----------
def _row_to_trade(d: dict) -> dict:
    for c in JSON_COLS:
        d[c] = json.loads(d[c]) if d.get(c) else ({} if c != "reasons" else [])
    return d


def _prep(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in TRADE_COLS:
            raise ValueError(f"bilinmeyen sütun: {k}")
        out[k] = json.dumps(v, default=_clean) if k in JSON_COLS and not isinstance(v, str) else v
    return out


def insert_trade(t: dict) -> int:
    t = _prep(t)
    cols = ",".join(t)
    vals = ",".join(f":{k}" for k in t)
    if _pg():
        return int(_exec(f"INSERT INTO smcbot_trades({cols}) VALUES({vals}) RETURNING id", t).scalar_one())
    return int(_exec(f"INSERT INTO smcbot_trades({cols}) VALUES({vals})", t).lastrowid)


def update_trade(trade_id: int, **fields) -> None:
    if not fields:
        return
    f = _prep(fields)
    sets = ",".join(f"{k}=:{k}" for k in f)
    _exec(f"UPDATE smcbot_trades SET {sets} WHERE id=:_id", {**f, "_id": trade_id})


def get_trade(trade_id: int) -> dict | None:
    rows = _rows("SELECT * FROM smcbot_trades WHERE id=:i", {"i": trade_id})
    return _row_to_trade(rows[0]) if rows else None


def list_trades(mode: str, statuses: tuple[str, ...] | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    q = "SELECT * FROM smcbot_trades WHERE mode=:mode"
    p: dict[str, Any] = {"mode": mode, "lim": int(limit), "off": int(offset)}
    if statuses:
        names = []
        for i, st in enumerate(statuses):
            p[f"s{i}"] = st
            names.append(f":s{i}")
        q += f" AND status IN ({','.join(names)})"
    q += " ORDER BY COALESCE(exit_time, entry_time, setup_time) DESC, id DESC LIMIT :lim OFFSET :off"
    return [_row_to_trade(r) for r in _rows(q, p)]


def count_trades(mode: str, status: str) -> int:
    return int(_rows("SELECT COUNT(*) AS n FROM smcbot_trades WHERE mode=:m AND status=:s",
                     {"m": mode, "s": status})[0]["n"])


def delete_mode_data(mode: str) -> None:
    _exec("DELETE FROM smcbot_trades WHERE mode=:m", {"m": mode})
    _exec("DELETE FROM smcbot_equity WHERE mode=:m", {"m": mode})


# ---------- equity ----------
def add_equity_point(mode: str, balance: float, equity: float, ts: int | None = None) -> None:
    _exec("INSERT INTO smcbot_equity(mode,ts,balance,equity) VALUES(:m,:t,:b,:e)",
          {"m": mode, "t": int(ts or now_ms()), "b": round(float(balance), 4), "e": round(float(equity), 4)})


def equity_series(mode: str, limit: int = 3000) -> list[dict]:
    return _rows("SELECT ts,balance,equity FROM (SELECT ts,balance,equity FROM smcbot_equity WHERE mode=:m "
                 "ORDER BY ts DESC LIMIT :l) AS sub ORDER BY ts ASC", {"m": mode, "l": int(limit)})


# ---------- logs ----------
def add_log(level: str, msg: str) -> None:
    global _log_inserts
    _exec("INSERT INTO smcbot_logs(ts,level,msg) VALUES(:t,:l,:m)", {"t": now_ms(), "l": level, "m": msg[:2000]})
    _log_inserts += 1
    if _log_inserts % 200 == 0:  # tabloyu sınırlı tut
        _exec("DELETE FROM smcbot_logs WHERE id < (SELECT COALESCE(MAX(id),0) - 5000 FROM smcbot_logs)")


def get_logs(after_id: int = 0, limit: int = 200) -> list[dict]:
    return _rows("SELECT * FROM (SELECT id,ts,level,msg FROM smcbot_logs WHERE id>:a ORDER BY id DESC LIMIT :l) "
                 "AS sub ORDER BY id ASC", {"a": int(after_id), "l": int(limit)})


def clear_logs() -> None:
    _exec("DELETE FROM smcbot_logs")


# ---------- backtests ----------
def save_backtest(params: dict, result: dict) -> int:
    p = {"t": now_ms(), "p": json.dumps(params, default=_clean), "r": json.dumps(result, default=_clean)}
    if _pg():
        new_id = int(_exec("INSERT INTO smcbot_backtests(ts,params,result) VALUES(:t,:p,:r) RETURNING id", p).scalar_one())
    else:
        new_id = int(_exec("INSERT INTO smcbot_backtests(ts,params,result) VALUES(:t,:p,:r)", p).lastrowid)
    _exec("DELETE FROM smcbot_backtests WHERE id < :keep", {"keep": new_id - 5})
    return new_id


def last_backtest() -> dict | None:
    rows = _rows("SELECT * FROM smcbot_backtests ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    return {"id": r["id"], "ts": r["ts"], "params": json.loads(r["params"]), "result": json.loads(r["result"])}


# ---------- yönetim kilidi ----------
# Render yeni sürümü açarken eski kopyayı bir süre daha çalıştırır. Aynı anda iki kopyanın
# işlem açmaması için yalnızca kilidi tutan kopya işlem yapar.
def lease_acquire(name: str, owner: str, ttl_ms: int) -> bool:
    now = now_ms()
    _exec("INSERT INTO smcbot_lease(name,owner,until) VALUES(:n,'',0) ON CONFLICT(name) DO NOTHING", {"n": name})
    res = _exec("UPDATE smcbot_lease SET owner=:o, until=:u WHERE name=:n AND (owner=:o OR until < :now)",
                {"n": name, "o": owner, "u": now + ttl_ms, "now": now})
    return res.rowcount == 1


def lease_release(name: str, owner: str) -> None:
    _exec("UPDATE smcbot_lease SET until=0 WHERE name=:n AND owner=:o", {"n": name, "o": owner})


def lease_info(name: str) -> dict | None:
    rows = _rows("SELECT owner, until FROM smcbot_lease WHERE name=:n", {"n": name})
    return rows[0] if rows else None


def wipe_all() -> None:
    """Sadece testler için."""
    for t in ("smcbot_kv", "smcbot_trades", "smcbot_equity", "smcbot_logs", "smcbot_backtests", "smcbot_lease"):
        _exec(f"DELETE FROM {t}")
