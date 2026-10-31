"""Restart / reconnect sonrası Database State vs Binance State karşılaştırması.

Otomatik çözülen güvenli durumlar:
- DB'de hiç gönderilmemiş (SIGNAL_DETECTED/RISK_CHECK) pozisyon → ORDER_REJECTED
- DB'de açık, borsada kapalı pozisyon (bot kapalıyken SL/TP tetiklenmiş) → finalize
- Açık pozisyonda SL eksik → SL yeniden yerleştirilir
- Pozisyonu olmayan sembolde botun kendi (sb* prefix) emirleri → iptal

Manuel çözüm gerektiren durumlar (bot RECONCILIATION_REQUIRED'a geçer, yeni işlem açmaz):
- Borsada DB'de olmayan (orphan) pozisyon
- Yön / miktar uyuşmazlığı
- Durumu belirsiz entry emri
- SL'nin yeniden yerleştirilememesi
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.smc.core.types import Side
from backend.smc.database.repo import Repo
from backend.smc.execution.gateway import ExchangeGateway
from backend.smc.execution.state_machine import LIVE_STATES, PosState
from backend.smc.execution.trade_manager import TradeManager

S = PosState
BOT_PREFIX = "sb"


@dataclass
class ReconcileReport:
    issues: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.issues


async def reconcile(gw: ExchangeGateway, repo: Repo, tm: TradeManager) -> ReconcileReport:
    rep = ReconcileReport()
    ex_positions = {p.symbol: p for p in await gw.positions()}
    db_open = repo.open_positions()
    db_symbols = set()

    for pos in db_open:
        st = PosState(pos.state)
        side = Side(pos.side)
        if st in (S.SIGNAL_DETECTED, S.RISK_CHECK):
            repo.transition(pos.id, S.ORDER_REJECTED, exit_reason="NEVER_SUBMITTED")
            rep.actions.append(f"{pos.symbol}#{pos.id}: gönderilmemiş pozisyon kapatıldı")
            continue
        db_symbols.add(pos.symbol)
        ex = ex_positions.get(pos.symbol)
        if st == S.ORDER_SUBMITTED:
            entry = repo.active_order(pos.id, "ENTRY")
            info = await gw.get_order(pos.symbol, entry.client_id) if entry else None
            if info is not None and info.active:
                await gw.cancel(pos.symbol, info.client_id)
                info = await gw.get_order(pos.symbol, info.client_id) or info
            if (info is None or info.filled_qty == 0) and ex is None:
                repo.transition(pos.id, S.CLOSED, exit_reason="ENTRY_NOT_FILLED", open_risk=0.0)
                rep.actions.append(f"{pos.symbol}#{pos.id}: dolmamış entry kapatıldı")
            else:
                repo.update_position(pos.id, state=S.RECONCILIATION_REQUIRED.value)
                rep.issues.append(f"{pos.symbol}#{pos.id}: entry durumu belirsiz (filled="
                                  f"{getattr(info, 'filled_qty', '?')}, borsa={getattr(ex, 'qty', 0)})")
            continue
        if st not in LIVE_STATES:
            continue
        if ex is None:
            await tm.finalize(pos, await tm._exit_reason(pos))
            rep.actions.append(f"{pos.symbol}#{pos.id}: bot kapalıyken kapanmış pozisyon finalize edildi")
            continue
        if (ex.qty > 0) != (side is Side.LONG):
            repo.transition(pos.id, S.RECONCILIATION_REQUIRED)
            rep.issues.append(f"{pos.symbol}#{pos.id}: yön uyuşmazlığı (borsa {ex.qty})")
            continue
        step = float(tm.filters(pos.symbol).step_size)
        if abs(ex.qty) > pos.remaining_qty + step / 2:
            repo.transition(pos.id, S.RECONCILIATION_REQUIRED)
            rep.issues.append(f"{pos.symbol}#{pos.id}: borsa miktarı {abs(ex.qty)} > DB {pos.remaining_qty}")
            continue
        open_ids = {o.client_id for o in await gw.open_orders(pos.symbol)}
        sl = repo.active_order(pos.id, "SL")
        if sl is None or sl.client_id not in open_ids:
            ok = await tm._place_sl(pos, pos.stop_price, abs(ex.qty))
            if ok:
                rep.actions.append(f"{pos.symbol}#{pos.id}: eksik SL yeniden yerleştirildi")
            else:
                repo.transition(pos.id, S.RECONCILIATION_REQUIRED)
                rep.issues.append(f"{pos.symbol}#{pos.id}: SL yerleştirilemedi")
                continue
        if st == S.RECONCILIATION_REQUIRED:
            # önceki çalıştırmada işaretlenmiş ama artık tutarlı: yönetime geri al
            repo.update_position(pos.id, state=(S.BREAKEVEN_ACTIVE if pos.be_active else S.ACTIVE).value)
            rep.actions.append(f"{pos.symbol}#{pos.id}: reconciliation çözüldü")
        # Miktar azalmışsa (TP dolmuş) normal yönetim döngüsü işler
        await tm.manage(repo.get_position(pos.id), ex)

    shared = getattr(tm.cfg, "account_scope", "exclusive") == "shared"
    for sym, ex in ex_positions.items():
        if sym not in db_symbols:
            if shared:
                # Ortak hesap: başka bir motora (mevcut botun scalp/day/swing'i) ait olabilir.
                # SMC bu sembolde işlem açmaz ve pozisyona dokunmaz.
                rep.actions.append(f"{sym}: SMC'ye ait olmayan pozisyon (qty={ex.qty}) — dokunulmadı")
            else:
                rep.issues.append(f"{sym}: DB'de kaydı olmayan (orphan) pozisyon qty={ex.qty}")

    # Pozisyonu olmayan sembollerde kalmış bot emirleri
    try:
        orphan_orders = [o for o in await gw.open_orders() if o.symbol not in ex_positions
                         and o.client_id.startswith(BOT_PREFIX)]
    except Exception as exc:
        rep.issues.append(f"açık emirler okunamadı: {exc!r}")
        orphan_orders = []
    for o in orphan_orders:
        await gw.cancel(o.symbol, o.client_id, o.is_algo)
        rep.actions.append(f"{o.symbol}: yetim emir iptal edildi {o.client_id}")
    return rep
