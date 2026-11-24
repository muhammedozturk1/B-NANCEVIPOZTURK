"""Volatiliteye göre ayarlı momentum long-short + volatilite hedefleme (portföy stratejisi).

Saf fonksiyonlar: borsa/DB bilmez. Canlı bot (bot.py) ve geçmiş test aynı kodu kullanır.

Adımlar (her rebalance'ta, yalnız KAPANMIŞ 1s mumlarla):
1. Skor: 7, 14, 28 günlük getiri / aynı penceredeki volatilite; üçünün ortalaması.
2. Filtre: 24s hacim, funding (long'da yüksek pozitif, short'ta aşırı negatif funding'den kaçın).
3. Seçim: uygun coinler içinde en yüksek skorlu %25 → long, en düşük %25 → short, ortadakiler tutulmaz.
4. Ağırlık: risk paritesi (her pozisyon ≈ eşit volatilite) → 30 günlük kovaryansla portföy volatilitesi →
   ölçek = hedef_vol / gerçekleşen_vol → coin başı tavan, işlem başı risk tavanı, toplam kaldıraç tavanı.
5. Minimum notional: borsanın minimumunun altındaki pozisyon açılmaz (veya ayara göre tavanı aşmıyorsa yukarı yuvarlanır).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

HOURS_PER_YEAR = 24 * 365
DEFAULT_UNIVERSE = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,AVAXUSDT,LINKUSDT,DOGEUSDT"


@dataclass
class MomentumConfig:
    universe: list[str] = field(default_factory=lambda: DEFAULT_UNIVERSE.split(","))
    windows_days: tuple[int, ...] = (7, 14, 28)
    top_frac: float = 0.25
    target_vol: float = 0.20          # yıllık portföy volatilitesi hedefi
    vol_lookback_days: int = 30
    max_leverage: float = 1.5         # toplam nominal / özsermaye
    max_coin_pct: float = 15.0        # tek coinde en fazla nominal (% özsermaye)
    trade_risk_pct: float = 1.5       # stop olursa tek pozisyonda en fazla kayıp (% özsermaye)
    stop_vol_mult: float = 3.0        # koruma stopu = giriş ± k × günlük volatilite
    funding_long_max: float = 0.0003  # 8 saatlik funding bunun üstündeyse long açma (%0,03)
    funding_short_min: float = -0.0003
    min_volume_usd: float = 50_000_000.0
    min_notional_policy: str = "skip"  # skip | round_up (tavanları aşmıyorsa minimuma yükselt)
    rebalance_band_pct: float = 2.0    # hedefle fark özsermayenin bu %'sinden küçükse işlem yapma
    require_sign: bool = False         # True: long için skor > 0, short için skor < 0 şartı (şartnamede yok)

    @classmethod
    def from_settings(cls, s: dict) -> "MomentumConfig":
        uni = [x.strip().upper() for x in str(s.get("mom_universe", DEFAULT_UNIVERSE)).split(",") if x.strip()]
        try:
            win = tuple(int(x) for x in str(s.get("mom_windows", "7,14,28")).split(",") if x.strip())
        except ValueError:
            win = (7, 14, 28)
        return cls(
            universe=uni, windows_days=win or (7, 14, 28),
            top_frac=float(s.get("mom_top_frac", 0.25)),
            target_vol=float(s.get("mom_target_vol_pct", 20.0)) / 100,
            vol_lookback_days=int(s.get("mom_vol_lookback_days", 30)),
            max_leverage=float(s.get("mom_max_leverage", 1.5)),
            max_coin_pct=float(s.get("mom_max_coin_pct", 15.0)),
            trade_risk_pct=float(s.get("mom_trade_risk_pct", 1.5)),
            stop_vol_mult=float(s.get("mom_stop_vol_mult", 3.0)),
            funding_long_max=float(s.get("mom_funding_long_max_pct", 0.03)) / 100,
            funding_short_min=float(s.get("mom_funding_short_min_pct", -0.03)) / 100,
            min_volume_usd=float(s.get("mom_min_volume_musd", 50.0)) * 1e6,
            min_notional_policy=str(s.get("mom_min_notional_policy", "skip")),
            rebalance_band_pct=float(s.get("mom_rebalance_band_pct", 2.0)),
            require_sign=_flag(s.get("mom_require_sign", True)),
        )

    @property
    def history_hours(self) -> int:
        return 24 * max(max(self.windows_days), self.vol_lookback_days) + 1


def _flag(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "evet", "on")


# ---------------------------------------------------------------- skor
def score(closes: np.ndarray, windows_days=(7, 14, 28)) -> float | None:
    """closes: kapanmış 1s kapanışlar (en eskiden en yeniye). Her pencere için getiri / pencere volatilitesi."""
    c = np.asarray(closes, dtype=float)
    vals = []
    for d in windows_days:
        h = 24 * d
        if len(c) <= h or not np.all(np.isfinite(c[-h - 1:])) or np.any(c[-h - 1:] <= 0):
            return None
        seg = c[-h - 1:]
        r = np.diff(np.log(seg))
        vol = float(r.std(ddof=1) * math.sqrt(h))  # pencere boyunca beklenen oynaklık
        if vol <= 0:
            return None
        vals.append((seg[-1] / seg[0] - 1) / vol)
    return float(np.mean(vals))


def hourly_returns(closes: np.ndarray, hours: int) -> np.ndarray | None:
    c = np.asarray(closes, dtype=float)
    if len(c) <= hours:
        return None
    r = np.diff(np.log(c[-hours - 1:]))
    return r if np.all(np.isfinite(r)) else None


def daily_vol(closes: np.ndarray, days: int = 30) -> float | None:
    r = hourly_returns(closes, 24 * days)
    return None if r is None else float(r.std(ddof=1) * math.sqrt(24))


# ---------------------------------------------------------------- seçim
@dataclass
class Selection:
    longs: list[str]
    shorts: list[str]
    scores: dict[str, float]
    skipped: dict[str, str]           # coin -> neden


def select(scores: dict[str, float | None], funding: dict[str, float | None], volume_usd: dict[str, float | None],
           cfg: MomentumConfig) -> Selection:
    """Uygun coinleri skora göre sırala; üst %25 long, alt %25 short. Filtreye takılan aday atlanır
    (yerine sıradaki coin GEÇMEZ: ortadaki coinler tanım gereği tutulmaz)."""
    skipped: dict[str, str] = {}
    ok: dict[str, float] = {}
    for sym, sc in scores.items():
        if sc is None:
            skipped[sym] = "yetersiz fiyat geçmişi"
            continue
        v = volume_usd.get(sym)
        if v is not None and v < cfg.min_volume_usd:
            skipped[sym] = f"24s hacim {v / 1e6:.0f}M$ < {cfg.min_volume_usd / 1e6:.0f}M$"
            continue
        ok[sym] = sc
    n = int(math.floor(len(ok) * cfg.top_frac + 1e-9))
    if n < 1 or len(ok) < 2 * n:
        return Selection([], [], ok, skipped)
    ranked = sorted(ok, key=lambda s: ok[s], reverse=True)
    longs, shorts = [], []
    for sym in ranked[:n]:
        f = funding.get(sym)
        if f is not None and f > cfg.funding_long_max:
            skipped[sym] = f"funding %{f * 100:.3f} > %{cfg.funding_long_max * 100:.3f} (long pahalı)"
        elif cfg.require_sign and ok[sym] <= 0:
            skipped[sym] = f"skor {ok[sym]:.2f} ≤ 0 (en iyi ama momentum negatif)"
        else:
            longs.append(sym)
    for sym in ranked[-n:]:
        f = funding.get(sym)
        if f is not None and f < cfg.funding_short_min:
            skipped[sym] = f"funding %{f * 100:.3f} < %{cfg.funding_short_min * 100:.3f} (short pahalı)"
        elif cfg.require_sign and ok[sym] >= 0:
            skipped[sym] = f"skor {ok[sym]:.2f} ≥ 0 (en kötü ama momentum pozitif)"
        else:
            shorts.append(sym)
    return Selection(longs, shorts, ok, skipped)


# ---------------------------------------------------------------- ağırlık
@dataclass
class Targets:
    weights: dict[str, float]         # işaretli nominal / özsermaye (long +, short −)
    stop_pct: dict[str, float]        # koruma stopu mesafesi (fiyatın oranı)
    port_vol: float                   # ölçek öncesi (birim) portföyün yıllık volatilitesi
    scale: float
    notes: list[str]


def target_weights(sel: Selection, closes: dict[str, np.ndarray], cfg: MomentumConfig) -> Targets:
    """Risk paritesi + volatilite hedefi + tavanlar."""
    syms = sel.longs + sel.shorts
    notes: list[str] = []
    if not syms:
        return Targets({}, {}, 0.0, 0.0, notes)
    H = 24 * cfg.vol_lookback_days
    rets, keep = [], []
    for s in syms:
        r = hourly_returns(closes[s], H)
        if r is None or r.std() <= 0:
            notes.append(f"{s}: {cfg.vol_lookback_days} günlük veri yok, atlandı")
            continue
        rets.append(r)
        keep.append(s)
    if not keep:
        return Targets({}, {}, 0.0, 0.0, notes)
    R = np.vstack(rets)                                    # coin × saat
    vol_ann = R.std(axis=1, ddof=1) * math.sqrt(HOURS_PER_YEAR)
    sign = np.array([1.0 if s in sel.longs else -1.0 for s in keep])
    w = sign / vol_ann                                      # risk paritesi: her coin ≈ eşit volatilite
    w = w / np.abs(w).sum()
    cov = np.atleast_2d(np.cov(R, ddof=1)) * HOURS_PER_YEAR
    pvol = float(math.sqrt(max(float(w @ cov @ w), 1e-12)))
    scale = cfg.target_vol / pvol
    w = w * scale
    dvol = vol_ann / math.sqrt(365)
    stop = cfg.stop_vol_mult * dvol
    cap_coin = cfg.max_coin_pct / 100
    cap_risk = (cfg.trade_risk_pct / 100) / stop           # stop olursa en fazla trade_risk kayıp
    for i, s in enumerate(keep):
        lim = min(cap_coin, float(cap_risk[i]))
        if abs(w[i]) > lim:
            notes.append(f"{s}: %{abs(w[i]) * 100:.1f} → %{lim * 100:.1f} ({'coin tavanı' if lim == cap_coin else 'işlem riski tavanı'})")
            w[i] = math.copysign(lim, w[i])
    gross = float(np.abs(w).sum())
    if gross > cfg.max_leverage:
        notes.append(f"toplam kaldıraç {gross:.2f}x → {cfg.max_leverage:.2f}x")
        w = w * (cfg.max_leverage / gross)
    return Targets({s: float(w[i]) for i, s in enumerate(keep)}, {s: float(stop[i]) for i, s in enumerate(keep)},
                   pvol, scale, notes)


def apply_min_notional(t: Targets, equity: float, min_notional: dict[str, float], cfg: MomentumConfig) -> list[str]:
    """Borsa minimumunun altındaki hedefleri kaldır (skip) ya da tavanları aşmıyorsa minimuma yükselt (round_up).
    Değişiklik yerinde yapılır; açıklamalar döner."""
    out = []
    for s in list(t.weights):
        notional = abs(t.weights[s]) * equity
        mn = float(min_notional.get(s) or 5.0) * 1.05   # fiyat oynamasına karşı %5 pay
        if notional >= mn:
            continue
        lim = min(cfg.max_coin_pct / 100, (cfg.trade_risk_pct / 100) / t.stop_pct[s]) * equity
        if cfg.min_notional_policy == "round_up" and mn <= lim:
            t.weights[s] = math.copysign(mn / equity, t.weights[s])
            out.append(f"{s}: {notional:.2f}$ → borsa minimumu {mn:.2f}$'a yükseltildi")
        else:
            out.append(f"{s}: hedef {notional:.2f}$ < borsa minimumu {mn:.2f}$ (izin verilen en fazla {lim:.2f}$) → açılmadı")
            del t.weights[s]
            del t.stop_pct[s]
    return out


def plan_orders(targets: dict[str, float], current: dict[str, float], equity: float, prices: dict[str, float],
                min_notional: dict[str, float], cfg: MomentumConfig) -> list[dict]:
    """Hedef nominale ulaşmak için emirler. current: işaretli mevcut nominal ($).
    Önce azaltma/kapatma (marjin boşalsın), sonra artırma/açma. Küçük farklar (band) ve borsa minimumunun
    altındaki ayarlamalar yapılmaz; ama tamamen kapatma her zaman yapılır."""
    band = cfg.rebalance_band_pct / 100 * equity
    orders = []
    for s in sorted(set(targets) | set(current)):
        tgt = targets.get(s, 0.0) * equity
        cur = current.get(s, 0.0)
        if tgt == 0 and cur != 0:
            orders.append({"symbol": s, "action": "close", "delta": -cur, "target": 0.0})
            continue
        if cur != 0 and math.copysign(1, tgt) != math.copysign(1, cur):
            orders.append({"symbol": s, "action": "close", "delta": -cur, "target": 0.0})
            orders.append({"symbol": s, "action": "open", "delta": tgt, "target": tgt})
            continue
        delta = tgt - cur
        mn = float(min_notional.get(s) or 5.0)
        if abs(delta) < max(band, mn):
            continue
        if cur == 0:
            orders.append({"symbol": s, "action": "open", "delta": delta, "target": tgt})
        elif abs(tgt) < abs(cur):
            orders.append({"symbol": s, "action": "reduce", "delta": delta, "target": tgt})
        else:
            orders.append({"symbol": s, "action": "add", "delta": delta, "target": tgt})
    rank = {"close": 0, "reduce": 1, "open": 2, "add": 2}
    return sorted(orders, key=lambda o: rank[o["action"]])
