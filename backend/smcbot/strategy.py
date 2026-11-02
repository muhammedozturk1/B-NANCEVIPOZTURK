"""SMC/ICT yapısı + order flow -> puanlanmış işlem sinyali.

Her sinyal, panelde gösterilmek üzere neden girildiğini madde madde açıklar
ve grafiğe çizilecek seviyeleri (av, MSS, FVG, hedef) taşır.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .orderflow import FlowSnapshot, est_liq_clusters
from .smc import Analysis, Candles, HTFContext, RawSetup, find_long, in_killzone, liquidity_targets

WORDS = {
    "LONG": {"low": "dip", "lows": "dipler", "swept_liq": "sell-side likidite", "target_liq": "buy-side likidite",
             "pd": "PDL", "pd_name": "önceki gün dibi", "trend": "yukarı", "liq_victim": "long",
             "crowd": "short", "zone": "indirim (discount)", "htf_pd": "PDH", "t_eq": "eşit tepeler",
             "t_sw": "swing tepesi", "t_pd": "önceki gün tepesi (PDH)", "t_htf": "üst zaman dilimi tepesi",
             "t_liq": "short likidasyon kümesi", "bid": "alış"},
    "SHORT": {"low": "tepe", "lows": "tepeler", "swept_liq": "buy-side likidite", "target_liq": "sell-side likidite",
              "pd": "PDH", "pd_name": "önceki gün tepesi", "trend": "aşağı", "liq_victim": "short",
              "crowd": "long", "zone": "pahalı bölge (premium)", "htf_pd": "PDL", "t_eq": "eşit dipler",
              "t_sw": "swing dibi", "t_pd": "önceki gün dibi (PDL)", "t_htf": "üst zaman dilimi dibi",
              "t_liq": "long likidasyon kümesi", "bid": "satış"},
}


def _tr(text: str) -> str:
    """1,234.56 -> 1.234,56 (Türkçe biçim)."""
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def n(v: float, d: int = 1) -> str:
    return _tr(f"{v:.{d}f}")


def fmt_price(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        return _tr(f"{v:,.1f}")
    if a >= 10:
        return _tr(f"{v:,.2f}")
    if a >= 1:
        return _tr(f"{v:.4f}")
    return _tr(f"{v:.6g}")


def fmt_usd(v: float) -> str:
    if v >= 1e6:
        return f"{n(v / 1e6, 2)}M$"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K$"
    return f"{v:.0f}$"


@dataclass
class Signal:
    symbol: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    stop_pct: float
    score: float
    structure_score: float
    flow_score: float
    setup_time: int
    expiry_time: int
    key: str
    reasons: list = field(default_factory=list)
    annotations: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SymbolAnalyzer:
    """Bir coinin LTF + HTF mumlarını analiz eder; long ve short'u simetrik değerlendirir."""

    def __init__(self, symbol: str, ltf: Candles, htf: Candles):
        self.symbol = symbol
        self.ltf = ltf
        self.views = {
            "LONG": (Analysis(ltf), HTFContext(htf)),
            "SHORT": (Analysis(ltf.mirror()), HTFContext(htf.mirror())),
        }

    def evaluate(self, t: int, s: dict, flow: FlowSnapshot | None = None,
                 min_score: float | None = None) -> tuple[Signal | None, Signal | None]:
        """(eşiği geçen en iyi sinyal, eşik altı en iyi aday) döner."""
        best = None
        for side, (A, H) in self.views.items():
            raw = find_long(A, t, s["min_stop_pct"], s["max_stop_pct"], s.get("entry_mode", "ce"))
            if raw is None:
                continue
            fl = None if flow is None else (flow if side == "LONG" else flow.mirrored())
            sig = self._build(side, A, H, raw, fl, s)
            if sig and (best is None or sig.score > best.score):
                best = sig
        thr = s["min_score"] if min_score is None else min_score
        if best and best.score >= thr:
            return best, None
        return None, best

    # ------------------------------------------------------------------
    def _build(self, side: str, A: Analysis, H: HTFContext, r: RawSetup, fl: FlowSnapshot | None, s: dict) -> Signal | None:
        c = A.c
        W = WORDS[side]
        mir = c.mirrored
        px = (lambda v: -v) if mir else (lambda v: v)  # noqa: E731
        reasons: list[dict] = []
        st = 0.0  # yapı puanı
        fs = 0.0  # akış puanı

        def add(points: float, text: str, ok: bool | None = True, flow: bool = False) -> None:
            nonlocal st, fs
            if flow:
                fs += points
            else:
                st += points
            reasons.append({"ok": ok, "text": text, "pts": int(round(points))})

        t = r.t
        close_time = int(c.time[t] + c.interval_ms)
        risk = r.entry - r.stop

        # 1) Üst zaman dilimi
        j = H.index_for(close_time)
        if j < 10:
            return None
        bias = int(H.bias[j])
        pos = H.position_in_range(j, r.entry)
        if bias == 1:
            add(20, f"Üst zaman dilimi trendi {W['trend']} (son yapı kırılımı/BOS bu yönde)")
        elif bias == 0:
            if pos < 0.5:
                add(8, f"Üst zaman dilimi kararsız, fiyat aralığın {W['zone']} yarısında")
            else:
                add(0, "Üst zaman dilimi kararsız ve fiyat aralığın ters yarısında", ok=False)
        else:
            if pos < 0.25:
                add(0, f"Karşı trend işlem: üst zaman dilimi ters yönde ama fiyat aralığın derin {W['zone']} bölgesinde", ok=False)
            else:
                return None

        # 2) Likidite avı
        if r.pool_kind == "equal":
            add(15, f"Likidite avı: eşit {W['lows']} ({fmt_price(px(r.pool_level))}) süpürüldü — {W['swept_liq']} alındı")
        elif r.pool_kind == "pdl":
            add(15, f"Likidite avı: {W['pd_name']} ({W['pd']} {fmt_price(px(r.pool_level))}) süpürüldü")
        else:
            add(8, f"Likidite avı: swing {W['low']} ({fmt_price(px(r.pool_level))}) süpürüldü")
        if r.wick_sweep:
            add(5, "Av iğneyle yapıldı, mum seviyenin içinde kapandı (reddedilme)")

        # 3) MSS + displacement
        if r.displacement >= 1.0:
            add(10, f"MSS: {fmt_price(px(r.mss_level))} güçlü gövdeyle kırıldı (displacement {n(r.displacement)}x ATR)")
        else:
            add(5, f"MSS: {fmt_price(px(r.mss_level))} kırıldı (orta güçte, {n(r.displacement)}x ATR)")

        # 4) Giriş bölgesi
        zt, zb = sorted((px(r.zone_top), px(r.zone_bottom)), reverse=True)
        em = s.get("entry_mode", "ce")
        how = {"ce": "ortası (CE)", "edge": "ilk temas (üst kenar)", "market": "MSS kapanışında piyasa girişi"}[em]
        if r.zone_type == "FVG":
            add(10, f"FVG {fmt_price(zb)} – {fmt_price(zt)}; giriş {how} {fmt_price(px(r.entry))}")
        else:
            add(5, f"Order block {fmt_price(zb)} – {fmt_price(zt)} (FVG yok); giriş {fmt_price(px(r.entry))}")
        if r.retrace >= 0.5:
            add(5, f"Giriş hareketin {W['zone']} yarısında (%{r.retrace * 100:.0f} geri çekilme)")

        # 5) CVD
        if r.cvd_div:
            add(10, f"CVD uyumsuzluğu: fiyat yeni {W['low']} yaptı ama CVD yapmadı (emilim)")
        elif r.sweep_delta_pos:
            add(4, "Av mumunda agresif karşı taraf hacmi baskın (delta ters döndü)")
        if r.leg_delta > 0:
            add(5, "Kırılım bacağında delta işlem yönünde (gerçek alım/satım desteği)")

        # 6) Killzone
        kz = in_killzone(int(c.time[t]), c.interval_ms)
        if kz:
            add(5, f"Seans: {kz}")

        # 7) Hedef (likidite)
        targets: list[tuple[float, str, int]] = list(liquidity_targets(A, t, r.entry))
        for lvl in H.unswept_highs(j, r.entry):
            targets.append((lvl, "htf", 2))
        est_above, est_below = est_liq_clusters(c, t)
        if est_above:
            wmax = max(v for _, v in est_above)
            for p, v in est_above:
                if p > r.entry and v >= 0.5 * wmax:
                    targets.append((p, "liq", 2))
        cg_used = False
        if fl and fl.cg_clusters_above:
            for p, v in fl.cg_clusters_above:
                if p > r.entry:
                    targets.append((p, "cg", 2))
                    cg_used = True
        targets.sort(key=lambda x: x[0])
        buf = 0.1 * r.atr
        tp2 = None
        tkind = ""
        tq = 0
        for lvl, kind, q in targets:
            rr = (lvl - buf - r.entry) / risk
            if rr < s["min_rr"]:
                continue
            if rr > 10:
                break
            tp2, tkind, tq = lvl - buf, kind, q
            break
        labels = {"equal": W["t_eq"], "swing": W["t_sw"], "pdh": W["t_pd"], "htf": W["t_htf"],
                  "liq": f"tahmini {W['t_liq']}", "cg": f"CoinGlass {W['t_liq']}"}
        if tp2 is None:
            if s.get("require_liquidity_target", True):
                return None
            tp2 = r.entry + s["min_rr"] * risk
            add(0, f"Net likidite hedefi yok, sabit {n(s['min_rr'])}R hedef", ok=False)
            rr = s["min_rr"]
        else:
            rr = (tp2 - r.entry) / risk
            msg = f"Hedef: {labels[tkind]} {fmt_price(px(tp2 + buf))} ({W['target_liq']}), RR {n(rr)}"
            add(5 if tq >= 2 else 0, msg)
            if rr >= 3:
                add(5, f"Ödül/risk oranı yüksek ({n(rr)}R)")

        # 8) Canlı order flow
        if fl is not None:
            base = max(fl.liq_baseline_usd * 2, 50_000)
            if fl.liq_against_usd >= base:
                mult = fl.liq_against_usd / max(fl.liq_baseline_usd, 1)
                add(10, f"Av sırasında {fmt_usd(fl.liq_against_usd)} {W['liq_victim']} likidasyonu (normalin {n(mult)} katı)", flow=True)
            elif fl.liq_against_usd > 0:
                add(0, f"Av sırasında {fmt_usd(fl.liq_against_usd)} {W['liq_victim']} likidasyonu (belirgin değil)", ok=None, flow=True)
            if fl.oi_change_pct is not None:
                if fl.oi_change_pct <= -0.5:
                    add(5, f"Open interest %{n(fl.oi_change_pct)} düştü (stop/likidasyon temizliği)", flow=True)
                else:
                    oi_txt = _tr(f"{fl.oi_change_pct:+.1f}")
                    add(0, f"Open interest değişimi %{oi_txt}", ok=None, flow=True)
            if fl.funding is not None:
                f_pct = (fl.funding if not mir else -fl.funding) * 100
                if fl.funding <= 0.00005:
                    add(5, f"Funding %{n(f_pct, 4)}: kalabalık {W['crowd']} tarafında, sıkışma (squeeze) lehimize", flow=True)
                elif fl.funding >= 0.0005:
                    add(-10, f"Funding %{n(f_pct, 4)}: piyasa zaten bizim yönde aşırı kalabalık", ok=False, flow=True)
            if fl.ob_imbalance is not None:
                if fl.ob_imbalance >= 0.15:
                    add(5, f"Emir defteri %1 aralıkta {W['bid']} ağırlıklı ({fl.ob_imbalance:+.0%})", flow=True)
                elif fl.ob_imbalance <= -0.25:
                    add(-5, f"Emir defteri ters yönde ağır ({fl.ob_imbalance:+.0%})", ok=False, flow=True)
        if cg_used:
            reasons.append({"ok": True, "text": "CoinGlass likidasyon ısı haritası hedef seçiminde kullanıldı", "pts": 0})

        score = float(max(0, min(100, st + fs)))
        tp1 = r.entry + s["tp1_r"] * risk
        # gerçek fiyatlara dönüş
        entry, stop, tp1_r, tp2_r = px(r.entry), px(r.stop), px(tp1), px(tp2)
        stop_pct = abs(entry - stop) / abs(entry) * 100
        clusters = [{"price": px(p), "w": v} for p, v in est_above + est_below]
        ann = {
            "tf": "",
            "pool": {"price": px(r.pool_level), "time": int(c.time[r.pool_idx]) if r.pool_idx >= 0 else None,
                     "label": {"equal": f"Eşit {W['lows']}", "pdl": W["pd"], "swing": f"Swing {W['low']}"}[r.pool_kind]},
            "sweep": {"time": int(c.time[r.s]), "price": px(c.low[r.s])},
            "mss": {"time": int(c.time[t]), "from_time": int(c.time[r.q]), "price": px(r.mss_level)},
            "zone": {"type": r.zone_type, "top": zt, "bottom": zb, "time": int(c.time[r.zone_time_idx])},
            "target": {"price": tp2_r, "label": labels.get(tkind, "Sabit RR")},
            "liq_clusters": clusters,
        }
        return Signal(
            symbol=self.symbol, side=side, entry=entry, stop=stop, tp1=tp1_r, tp2=tp2_r, rr=round(rr, 2),
            stop_pct=round(stop_pct, 3), score=round(score, 1), structure_score=round(st, 1), flow_score=round(fs, 1),
            setup_time=close_time, expiry_time=close_time + int(s["setup_expiry_bars"]) * c.interval_ms,
            key=f"{self.symbol}:{side}:{int(c.time[r.s])}", reasons=reasons, annotations=ann,
        )
