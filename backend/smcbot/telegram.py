"""Telegram bildirimleri."""
from __future__ import annotations

import html

import aiohttp

from . import config
from .strategy import _tr, fmt_price

MODE_TR = {"paper": "DEMO", "testnet": "TESTNET", "live": "GERÇEK"}


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


async def send(text: str) -> tuple[bool, str]:
    if not configured():
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil"
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                         "parse_mode": "HTML", "disable_web_page_preview": True}) as r:
                if r.status == 200:
                    return True, "ok"
                return False, f"Telegram {r.status}: {(await r.text())[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"Telegram hatası: {e}"


def _m(v: float, sign: bool = False) -> str:
    """Tutar, Türkçe biçimde: 1.234,56 / +12,30"""
    return _tr(f"{v:+,.2f}" if sign else f"{v:,.2f}")


def _e(s: str) -> str:
    return html.escape(str(s))


def msg_opened(t: dict, mode: str) -> str:
    icon = "🟢" if t["side"] == "LONG" else "🔴"
    trail = t.get("tp2") is None
    if trail:
        top = [r["text"] for r in (t.get("reasons") or []) if r.get("ok")][:2]
    else:
        top = [r["text"] for r in (t.get("reasons") or []) if r.get("ok") and r.get("pts", 0) >= 10][:4]
    lines = [
        f"{icon} <b>{t['side']} AÇILDI — {_e(t['symbol'])}</b>  <i>[SMC Bot · {MODE_TR.get(mode, mode)}]</i>",
        f"Giriş: <code>{fmt_price(t['entry_price'])}</code>  |  SL: <code>{fmt_price(t['sl'])}</code>",
    ]
    if trail:
        lines += ["Hedef yok: stop her 4 saatte iz sürer (trend takibi)",
                  f"Risk: {_m(t['risk_amount'])} USDT  |  {t['leverage']}x"]
    else:
        lines += [f"TP1: <code>{fmt_price(t['tp1'])}</code>  |  TP2: <code>{fmt_price(t['tp2'])}</code>",
                  f"Risk: {_m(t['risk_amount'])} USDT  |  {t['leverage']}x  |  Skor {(t.get('score') or 0):.0f}/100"]
    if top:
        lines.append("Neden:")
        lines += [f"• {_e(x)}" for x in top]
    return "\n".join(lines)


def msg_tp1(t: dict, mode: str, pnl: float) -> str:
    return (f"🎯 <b>TP1 — {_e(t['symbol'])} {t['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Pozisyonun %{t.get('tp1_close_pct', 50):.0f}'i kapandı: {_m(pnl, True)} USDT\n"
            f"Stop girişe çekildi (artık zarar riski yok)")


def msg_closed(t: dict, mode: str) -> str:
    r = t.get("r_multiple") or 0
    pnl = t.get("pnl") or 0
    icon = "✅" if pnl > 0 else ("⚪" if abs(r) < 0.05 else "❌")
    return (f"{icon} <b>KAPANDI — {_e(t['symbol'])} {t['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Sonuç: <b>{_m(pnl, True)} USDT ({_tr(f'{r:+.2f}')}R)</b>\n"
            f"Neden: {_e(t.get('exit_reason') or '-')}\n"
            f"Giriş {fmt_price(t['entry_price'])} → Çıkış {fmt_price(t.get('exit_price') or 0)}")


def msg_setup(sig: dict, mode: str) -> str:
    return (f"🔎 <b>Kurulum bulundu — {_e(sig['symbol'])} {sig['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Giriş: <code>{fmt_price(sig['entry'])}</code>" + (f" (skor {sig['score']:.0f})" if sig.get("score") is not None else ""))


def msg_alert(title: str, body: str) -> str:
    return f"⚠️ <b>SMC Bot · {_e(title)}</b>\n{_e(body)}"


def _r(v: float) -> str:
    return _tr(f"{v:+.2f}") + "R"


def msg_daily(d: dict) -> str:
    """Günlük özet: equity, bugün kapananlar, açık pozisyonlar, son tarama."""
    mode = MODE_TR.get(d["mode"], d["mode"])
    lines = [f"📊 <b>SMC Bot günlük özet — {_e(d['date'])}</b>  <i>[{mode}]</i>",
             f"Equity: <b>{_m(d['equity'])} USDT</b>  (açık K/Z {_m(d['upnl'], True)})"]
    start = d.get("start")
    if start:
        tot = d["equity"] - start
        pct = _tr(f"{tot / start * 100:+.1f}")
        lines.append(f"Toplam: {_m(tot, True)} USDT (%{pct})  ·  başlangıç {_m(start)}")
    closed = d.get("closed") or []
    day_pnl = sum(t.get("pnl") or 0 for t in closed)
    lines.append(f"\nBugün kapanan: {len(closed)} işlem, {_m(day_pnl, True)} USDT")
    for t in closed[:8]:
        pnl, r, why = t.get("pnl") or 0, t.get("r_multiple") or 0, _e(t.get("exit_reason") or "-")
        lines.append(f"• {_e(t['symbol'])} {t['side']}: {_m(pnl, True)} ({_r(r)}) — {why}")
    pos = d.get("positions") or []
    lines.append(f"\nAçık pozisyon: {len(pos)}/{d['max_open']}")
    for t in pos:
        u, ur = t.get("upnl") or 0, t.get("upnl_r") or 0
        lines.append(f"• {_e(t['symbol'])} {t['side']}: {_m(u, True)} ({_r(ur)}) · stop <code>{fmt_price(t['sl'])}</code>")
    if d["max_open"] and len(pos) >= d["max_open"]:
        lines.append("Limit dolu: yeni kırılımlar bir pozisyon kapanana kadar atlanır.")
    if d.get("scan_summary"):
        lines.append(f"\nSon tarama: {_e(d['scan_summary'])}")
    if d.get("halted"):
        lines.append(f"⛔ Risk kilidi: {_e(d['halted'])}")
    elif not d.get("running"):
        lines.append("⏸ Bot duraklatılmış: yeni işlem aranmıyor.")
    return "\n".join(lines)
