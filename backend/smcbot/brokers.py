"""İşlem yürütme: demo (paper) ve Binance (testnet / gerçek).

Gerçek modda her pozisyon açılır açılmaz borsaya STOP ve TP2 emirleri konur.
Böylece bot veya sunucu çökse bile pozisyon korumasız kalmaz.
Stop emri konamazsa pozisyon anında kapatılır.
"""
from __future__ import annotations

import asyncio
import re

from . import db
from .binance_client import BinanceError, BinanceREST, SymbolInfo


def _dir(side: str) -> int:
    return 1 if side == "LONG" else -1


class PaperBroker:
    """Gerçek piyasa fiyatlarıyla sanal işlem. Komisyon ve kayma dahil."""

    name = "paper"

    def __init__(self, settings_getter):
        self.s = settings_getter

    def balance(self) -> float:
        b = db.kv_get("paper:balance")
        if b is None:
            b = float(self.s()["paper_start_balance"])
            db.kv_set("paper:balance", b)
        return float(b)

    def _add(self, delta: float) -> None:
        db.kv_set("paper:balance", self.balance() + delta)

    def reset(self) -> None:
        b = float(self.s()["paper_start_balance"])
        db.kv_set("paper:balance", b)
        db.kv_set("paper:start_balance", b)  # Toplam K/Z bu değere göre (ayar sonradan değişse de)

    def start_balance(self) -> float:
        """Hesabın gerçekten başladığı bakiye. Eski kurulumlarda kayıt yoksa işlemlerden geri hesaplanır:
        bakiye = başlangıç + tüm işlemlerin net K/Z'si (açık işlemlerin giriş komisyonu dahil)."""
        v = db.kv_get("paper:start_balance")
        if v is None:
            pnl = sum(float(t.get("pnl") or 0) for t in db.list_trades("paper", ("open", "closed"), limit=100000))
            v = self.balance() - pnl
            if v <= 0:
                v = float(self.s()["paper_start_balance"])
            db.kv_set("paper:start_balance", round(v, 4))
        return float(v)

    async def get_balance(self) -> float:
        return self.balance()

    async def open(self, t: dict, qty: float, ref_price: float, sym: SymbolInfo | None) -> dict:
        s = self.s()
        slip = s["slippage_pct"] / 100
        price = ref_price * (1 + slip * _dir(t["side"]))
        fee = price * qty * s["taker_fee_pct"] / 100
        self._add(-fee)
        return {"price": price, "qty": qty, "fee": fee, "meta": {}}

    async def reduce(self, t: dict, qty: float, ref_price: float, kind: str, sym: SymbolInfo | None,
                     client_id: str | None = None) -> dict:
        """kind: 'tp' (limit gibi, kaymasız) | 'sl' / 'market' / 'partial' (piyasa emri: aleyhte kayma)."""
        s = self.s()
        slip = 0.0 if kind == "tp" else s["slippage_pct"] / 100
        price = ref_price * (1 - slip * _dir(t["side"]))
        fee = price * qty * s["taker_fee_pct"] / 100
        pnl = (price - t["entry_price"]) * qty * _dir(t["side"])
        self._add(pnl - fee)
        return {"price": price, "fee": fee, "pnl": pnl, "qty": qty}

    async def move_stop(self, t: dict, new_sl: float, sym: SymbolInfo | None) -> dict:
        return {}

    async def apply_funding(self, cost: float) -> None:
        self._add(-cost)

    async def sync(self, open_trades: list[dict]) -> list[dict]:
        return []

    async def recover(self, open_trades: list[dict], symbols: dict) -> list[tuple[dict, dict, str]]:
        return []


def _stop_mode(side: str) -> str:
    """Stop fiyatı koruma yönünde yuvarlanır: LONG yukarı, SHORT aşağı (yuvarlama stopu geri götürmez)."""
    return "up" if side == "LONG" else "down"


class LiveBroker:
    """Binance USDⓈ-M Futures (testnet veya gerçek). Tek yönlü (one-way) mod gerekir."""

    def __init__(self, name: str, rest: BinanceREST, settings_getter, log):
        self.name = name
        self.rest = rest
        self.s = settings_getter
        self.log = log
        self.hedge_mode: bool | None = None
        self._bal_cache = (0.0, 0.0)

    async def check(self) -> str | None:
        """Bağlantı ve hesap ayarlarını doğrular. Sorun varsa açıklama döner."""
        try:
            await self.rest.sync_time()
            self.hedge_mode = await self.rest.position_mode_is_hedge()
            if self.hedge_mode:
                return "Binance hesabı 'Hedge Mode'da. Bot için 'One-way Mode' gerekli (Vadeli > Tercihler > Pozisyon Modu)."
            await self.rest.balance_usdt()
        except BinanceError as e:
            return str(e)
        return None

    async def get_balance(self) -> float:
        b = await self.rest.balance_usdt()
        return b["wallet"]

    async def _cancel_own(self, meta: dict | None) -> None:
        """Hesap başka botlarla (SMC Scalper) paylaşılıyor: SADECE bu botun koyduğu koşullu emirleri iptal et."""
        for k in ("sl_algo", "tp_algo"):
            aid = (meta or {}).get(k)
            if aid:
                try:
                    await self.rest.cancel_algo(int(aid))
                except BinanceError as e:
                    self.log("warn", f"Koşullu emir {aid} iptal edilemedi: {e}")

    async def open(self, t: dict, qty: float, ref_price: float, sym: SymbolInfo | None) -> dict:
        if self.hedge_mode:
            raise BinanceError(0, None, "Hedge mode açık, işlem açılmadı")
        assert sym is not None
        s = self.s()
        symbol = t["symbol"]
        # Aynı hesapta bu coinde başka bir botun pozisyonu varsa girme (tek yönlü modda pozisyonlar birleşir)
        if any(p["symbol"] == symbol for p in await self.rest.positions()):
            raise BinanceError(0, None, "Bu coinde hesapta başka bir pozisyon var (SMC Scalper olabilir)")
        await self.rest.set_isolated(symbol)
        await self.rest.set_leverage(symbol, int(t.get("leverage") or s["leverage"]))
        side = "BUY" if t["side"] == "LONG" else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        res = await self.rest.market_order(symbol, side, sym.fmt_qty(qty), client_id=f"smc{t['id']}o")
        exec_qty = float(res.get("executedQty") or 0)
        avg = float(res.get("avgPrice") or 0) or ref_price
        if exec_qty <= 0:
            raise BinanceError(0, None, f"Market emri dolmadı: {res}")
        meta = {}
        try:
            sl = await self.rest.algo_close_order(symbol, close_side, "STOP_MARKET",
                                                  sym.fmt_price(t["sl"], _stop_mode(t["side"])), f"smc{t['id']}sl")
            meta["sl_algo"] = sl.get("algoId")
            if t.get("tp2") is not None:  # trend takibinde hedef yok, sadece (iz süren) stop
                tp = await self.rest.algo_close_order(symbol, close_side, "TAKE_PROFIT_MARKET", sym.fmt_price(t["tp2"]), f"smc{t['id']}tp")
                meta["tp_algo"] = tp.get("algoId")
        except BinanceError as e:
            self.log("error", f"{symbol}: koruma emri konamadı ({e}). Pozisyon güvenlik için kapatılıyor.")
            await self._cancel_own(meta)
            await self.rest.market_order(symbol, close_side, sym.fmt_qty(exec_qty), reduce_only=True)
            raise
        fee = avg * exec_qty * s["taker_fee_pct"] / 100
        return {"price": avg, "qty": exec_qty, "fee": fee, "meta": meta}

    async def reduce(self, t: dict, qty: float, ref_price: float, kind: str, sym: SymbolInfo | None,
                     client_id: str | None = None) -> dict:
        """Piyasa emriyle reduceOnly kapatma. kind='partial': verilen miktar (stop emri yerinde kalır,
        closePosition olduğu için kalan miktarı otomatik korur). Diğerleri: botun emirleri iptal + tüm pozisyon.
        client_id verilirse idempotenttir: aynı kimlik borsada zaten varsa yeniden gönderilmez, mevcut emir okunur."""
        assert sym is not None
        s = self.s()
        close_side = "SELL" if t["side"] == "LONG" else "BUY"
        if kind != "partial":
            await self._cancel_own(t.get("meta"))
            # borsadaki gerçek miktarı kullan
            for p in await self.rest.positions():
                if p["symbol"] == t["symbol"]:
                    qty = abs(float(p["positionAmt"]))
                    break
            else:
                qty = 0
        if qty <= 0:
            return {"price": ref_price, "fee": 0.0, "pnl": 0.0, "qty": 0.0}
        cid = client_id or f"smc{t['id']}r{int(asyncio.get_event_loop().time()) % 100000}"
        res = None
        if client_id:
            # Binance istemci kimliğini yalnız AÇIK emirler arasında benzersiz tutar: dolmuş bir piyasa emrinin
            # kimliği tekrar kabul edilebilir. Bu yüzden göndermeden önce aynı kimlikle emir var mı diye sorulur.
            try:
                prev = await self.rest.get_order(t["symbol"], client_id)
                if prev and float(prev.get("executedQty") or 0) > 0:
                    res = prev
                    self.log("warn", f"#{t['id']} {client_id} daha önce dolmuş; tekrar gönderilmedi")
            except BinanceError as e:
                if e.code not in (-2011, -2013):  # -2013: emir yok -> gönder
                    raise
        if res is None:
            try:
                res = await self.rest.market_order(t["symbol"], close_side, sym.fmt_qty(qty), reduce_only=True,
                                                   client_id=cid)
            except BinanceError as e:
                if client_id and e.code == -4116:  # aynı istemci kimliği açık emirde: mevcut emri oku
                    res = await self.rest.get_order(t["symbol"], client_id)
                    self.log("warn", f"#{t['id']} {client_id} zaten gönderilmiş; mevcut emir sonucu kullanıldı")
                else:
                    raise
        price = float(res.get("avgPrice") or 0) or ref_price
        q = float(res.get("executedQty") or 0)
        if q <= 0:
            raise BinanceError(0, None, f"Kapatma emri dolmadı: {res}")
        fee = price * q * s["taker_fee_pct"] / 100
        pnl = (price - t["entry_price"]) * q * _dir(t["side"])
        return {"price": price, "fee": fee, "pnl": pnl, "qty": q, "time": int(res.get("updateTime") or 0)}

    async def move_stop(self, t: dict, new_sl: float, sym: SymbolInfo | None) -> dict:
        """Önce yeni stop konur, sonra eskisi iptal edilir (arada pozisyon korumasız kalmaz).
        Eski stopun iptali başarısız olursa yeni stopun kimliği YİNE kaydedilir, eski kimlik stale_algos listesine
        yazılır ve mutabakatta (recover) tekrar iptal edilir. Yeni stop konamazsa BinanceError yükselir (eski stop yerinde)."""
        assert sym is not None
        close_side = "SELL" if t["side"] == "LONG" else "BUY"
        meta = dict(t.get("meta") or {})
        old = meta.get("sl_algo")
        seq = int(meta.get("sl_seq", 0)) + 1  # her stop taşımada benzersiz emir kimliği
        new = await self.rest.algo_close_order(t["symbol"], close_side, "STOP_MARKET",
                                               sym.fmt_price(new_sl, _stop_mode(t["side"])), f"smc{t['id']}s{seq}")
        meta["sl_algo"] = new.get("algoId")
        meta["sl_seq"] = seq
        if old:
            try:
                await self.rest.cancel_algo(int(old))
            except BinanceError as e:
                self.log("warn", f"#{t['id']} eski stop {old} iptal edilemedi ({e}); mutabakatta tekrar denenecek")
                meta["stale_algos"] = list(meta.get("stale_algos") or []) + [old]
        return meta

    async def recover(self, open_trades: list[dict], symbols: dict) -> list[tuple[dict, dict, str]]:
        """Yeniden başlatma / kopma sonrası mutabakat. Her açık işlem için:
        - borsadaki miktar DB'den farklıysa DB'yi borsaya eşitle (ör. kısmi kapatma dolmuş ama kaydedilmemiş)
        - botun stop emri borsada yoksa geçerli stop fiyatıyla YENİDEN koy
        - bu işleme ait fazladan (eski/çift) stop emirlerini ve stale_algos'u iptal et
        Dönüş: [(işlem, güncellenecek alanlar, açıklama)]. Pozisyonu olmayan işlemler sync()'e bırakılır."""
        if not open_trades:
            return []
        live = {p["symbol"]: p for p in await self.rest.positions()}
        algos = await self.rest.open_algo_orders()
        out = []
        for t in open_trades:
            p = live.get(t["symbol"])
            sym = symbols.get(t["symbol"])
            if not p or sym is None:
                continue
            meta = dict(t.get("meta") or {})
            upd: dict = {}
            notes = []
            amt = abs(float(p["positionAmt"]))
            pending = ((meta.get("exit") or {}).get("pending_partial") is not None)
            # yarım kalan kısmi kapatma varsa miktarı bot tamamlar (emir kimliğiyle sorgulayıp kaydeder): çift düşme olmasın
            if not pending and abs(amt - float(t.get("qty_open") or 0)) >= float(sym.step) / 2:
                upd["qty_open"] = amt
                notes.append(f"miktar {t.get('qty_open')} → {amt} (borsaya eşitlendi)")
            own = re.compile(rf"^smc{int(t['id'])}(sl|s\d+)$")  # smc1 ile smc12 karışmasın
            mine = [a for a in algos if a.get("symbol") == t["symbol"] and own.match(str(a.get("clientAlgoId", "")))]
            cur = meta.get("sl_algo")
            has_cur = any(int(a.get("algoId", 0)) == int(cur or 0) for a in mine)
            for a in mine:  # fazladan stoplar
                aid = int(a.get("algoId", 0))
                if aid != int(cur or 0) and a.get("orderType", a.get("type")) in ("STOP_MARKET", None):
                    try:
                        await self.rest.cancel_algo(aid)
                        notes.append(f"fazladan stop {aid} iptal edildi")
                    except BinanceError as e:
                        self.log("warn", f"#{t['id']} fazladan stop {aid} iptal edilemedi: {e}")
            stale_left = []
            for aid in meta.get("stale_algos") or []:
                if int(aid) == int(cur or 0):
                    continue
                try:
                    await self.rest.cancel_algo(int(aid))
                except BinanceError:
                    stale_left.append(aid)
            if (meta.get("stale_algos") or []) != stale_left:
                meta["stale_algos"] = stale_left
                upd["meta"] = meta
            if not has_cur:
                close_side = "SELL" if t["side"] == "LONG" else "BUY"
                seq = int(meta.get("sl_seq", 0)) + 1
                res = await self.rest.algo_close_order(t["symbol"], close_side, "STOP_MARKET",
                                                       sym.fmt_price(t["sl"], _stop_mode(t["side"])), f"smc{t['id']}s{seq}")
                meta["sl_algo"], meta["sl_seq"] = res.get("algoId"), seq
                upd["meta"] = meta
                notes.append(f"borsada stop yoktu: {t['sl']} seviyesine yeniden kondu")
            if upd:
                out.append((t, upd, "; ".join(notes)))
        return out

    async def apply_funding(self, cost: float) -> None:
        return None  # borsa kendisi uygular

    async def sync(self, open_trades: list[dict]) -> list[dict]:
        """Borsada kapanmış (stop/TP2/likidasyon/elle) pozisyonları tespit eder."""
        if not open_trades:
            return []
        live = {p["symbol"]: p for p in await self.rest.positions()}
        closed = []
        for t in open_trades:
            if t["symbol"] in live:
                continue
            fills = []
            try:
                fills = await self.rest.user_trades(t["symbol"], int(t["entry_time"]) - 1000)
            except BinanceError as e:
                self.log("warn", f"{t['symbol']} işlem geçmişi okunamadı: {e}")
            close_side = "SELL" if t["side"] == "LONG" else "BUY"
            last_bot = int((t.get("meta") or {}).get("last_reduce_time", 0))
            cl = [f for f in fills if f.get("side") == close_side and int(f.get("time", 0)) > last_bot
                  and int(f.get("time", 0)) >= int(t["entry_time"])]
            qty = sum(float(f["qty"]) for f in cl)
            price = (sum(float(f["price"]) * float(f["qty"]) for f in cl) / qty) if qty else (t.get("sl") or t["entry_price"])
            pnl = sum(float(f.get("realizedPnl", 0)) for f in cl)
            fee = sum(float(f.get("commission", 0)) for f in cl)
            await self._cancel_own(t.get("meta"))
            closed.append({"trade": t, "price": price, "pnl": pnl, "fee": fee, "qty": qty})
        return closed
