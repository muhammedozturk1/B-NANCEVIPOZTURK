"""Telegram bildirimleri."""
from __future__ import annotations

import html

import aiohttp

from . import config
from .strategy import fmt_price

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


def _e(s: str) -> str:
    return html.escape(str(s))


def msg_opened(t: dict, mode: str) -> str:
    icon = "🟢" if t["side"] == "LONG" else "🔴"
    top = [r["text"] for r in (t.get("reasons") or []) if r.get("ok") and r.get("pts", 0) >= 10][:4]
    lines = [
        f"{icon} <b>{t['side']} AÇILDI — {_e(t['symbol'])}</b>  <i>[SMC Bot · {MODE_TR.get(mode, mode)}]</i>",
        f"Giriş: <code>{fmt_price(t['entry_price'])}</code>  |  SL: <code>{fmt_price(t['sl'])}</code>",
        f"TP1: <code>{fmt_price(t['tp1'])}</code>  |  TP2: <code>{fmt_price(t['tp2'])}</code>",
        f"Risk: {t['risk_amount']:.2f} USDT  |  {t['leverage']}x  |  Skor {t['score']:.0f}/100",
    ]
    if top:
        lines.append("Neden:")
        lines += [f"• {_e(x)}" for x in top]
    return "\n".join(lines)


def msg_tp1(t: dict, mode: str, pnl: float) -> str:
    return (f"🎯 <b>TP1 — {_e(t['symbol'])} {t['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Pozisyonun %{t.get('tp1_close_pct', 50):.0f}'i kapandı: {pnl:+.2f} USDT\n"
            f"Stop girişe çekildi (artık zarar riski yok)")


def msg_closed(t: dict, mode: str) -> str:
    r = t.get("r_multiple") or 0
    pnl = t.get("pnl") or 0
    icon = "✅" if pnl > 0 else ("⚪" if abs(r) < 0.05 else "❌")
    return (f"{icon} <b>KAPANDI — {_e(t['symbol'])} {t['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Sonuç: <b>{pnl:+.2f} USDT ({r:+.2f}R)</b>\n"
            f"Neden: {_e(t.get('exit_reason') or '-')}\n"
            f"Giriş {fmt_price(t['entry_price'])} → Çıkış {fmt_price(t.get('exit_price') or 0)}")


def msg_setup(sig: dict, mode: str) -> str:
    return (f"🔎 <b>Kurulum bulundu — {_e(sig['symbol'])} {sig['side']}</b> [SMC Bot · {MODE_TR.get(mode, mode)}]\n"
            f"Giriş bekleniyor: <code>{fmt_price(sig['entry'])}</code> (skor {sig['score']:.0f})")


def msg_alert(title: str, body: str) -> str:
    return f"⚠️ <b>SMC Bot · {_e(title)}</b>\n{_e(body)}"
