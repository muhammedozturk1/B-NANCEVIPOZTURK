"""Trend işlemleri için çıkış motoru: sabit 1R, stop önceliği, kademeli kâr kilidi, kısmi TP, trailing.

Canlı bot (bot.py) ve geçmiş test (trend.simulate) AYNI fonksiyonları kullanır; böylece iki taraf
aynı kararı verir. Bu modül borsa/DB bilmez: sadece "ne yapılmalı" sorusunu yanıtlar.

Kurallar
- 1R = |giriş fiyatı - ilk stop|. Pozisyon boyunca değişmez (ExitState.one_r).
- Stop sadece koruma yönünde hareket eder: LONG yeni >= eski, SHORT yeni <= eski.
  Adaylar (ilk stop, kâr kilidi kademeleri, ATR trailing, swing trailing) içinden EN KORUYUCU olan seçilir.
- Kâr kilidi kademeleri: (tetik_R, kilit_R) çiftleri. Örn. 1.0:0 → +1R görülünce stop 0R'ye.
- Kısmi TP kademeleri: (tetik_R, ilk miktarın oranı). Her kademe bir kez çalışır.
- Trailing, en iyi R aktivasyon eşiğine ulaştıktan sonra başlar.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TRIGGER_SOURCES = ("mark_price", "last_price", "candle_close")
TRAIL_MODES = ("off", "swing", "atr", "both")
MANAGE_TFS = ("1h", "4h")


def parse_tiers(text, *, frac: bool = False) -> list[tuple[float, float]]:
    """'1.0:0, 1.5:0.5' -> [(1.0, 0.0), (1.5, 0.5)] (tetiğe göre sıralı). Hatalı metin ValueError verir.
    frac=True: ikinci değer 0-1 arası oran olmalı ve toplam 1'i geçmemeli (kısmi TP)."""
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        items = [tuple(map(float, x)) for x in text]
    else:
        s = str(text).strip()
        if s in ("", "-", "0", "off", "kapalı"):
            return []
        items = []
        for part in s.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            a, b = part.split(":")
            items.append((float(a), float(b)))
    items.sort(key=lambda x: x[0])
    for trig, val in items:
        if not (math.isfinite(trig) and math.isfinite(val)) or trig <= 0:
            raise ValueError(f"Geçersiz kademe: {trig}:{val}")
        if frac and not 0 < val <= 1:
            raise ValueError(f"Kısmi TP oranı 0-1 arası olmalı: {val}")
        if not frac and val >= trig:
            raise ValueError(f"Kilit seviyesi tetikten küçük olmalı: {trig}:{val}")
    if frac and sum(v for _, v in items) > 1.0 + 1e-9:
        raise ValueError("Kısmi TP oranlarının toplamı 1'i geçemez")
    return items


@dataclass
class ExitConfig:
    lock_tiers: list = field(default_factory=list)       # [(tetik_R, kilit_R)]
    partial_tiers: list = field(default_factory=list)    # [(tetik_R, oran)]
    trail_mode: str = "swing"                            # off | swing | atr | both
    trail_activate_r: float = 0.0                        # trailing bu R görüldükten sonra başlar (0 = hemen)
    trail_atr_mult: float = 3.0
    trail_swing_n: int = 10                              # 4s mum
    trigger_source: str = "mark_price"                   # kilit + kısmi TP tetik fiyatı
    manage_tf: str = "4h"                                # trailing güncelleme ve candle_close tetik sıklığı
    lock_fee_cover: bool = True                          # kilit seviyesine giriş+çıkış maliyetini ekle
    cost_frac: float = 0.0012                            # 2×taker + kayma (fiyat oranı)

    @classmethod
    def from_settings(cls, s: dict) -> "ExitConfig":
        src = s.get("exit_trigger_source", "mark_price")
        mode = s.get("trail_mode", "swing")
        tf = s.get("manage_tf", "4h")
        return cls(
            lock_tiers=parse_tiers(s.get("lock_tiers", "")),
            partial_tiers=parse_tiers(s.get("partial_tp_tiers", ""), frac=True),
            trail_mode=mode if mode in TRAIL_MODES else "swing",
            trail_activate_r=float(s.get("trail_activate_r", 0.0)),
            trail_atr_mult=float(s.get("trail_atr_mult", 3.0)),
            trail_swing_n=int(s.get("trend_exit_n", 10)),
            trigger_source=src if src in TRIGGER_SOURCES else "mark_price",
            manage_tf=tf if tf in MANAGE_TFS else "4h",
            lock_fee_cover=bool(s.get("lock_fee_cover", True)),
            cost_frac=(2 * float(s.get("taker_fee_pct", 0.05)) + float(s.get("slippage_pct", 0.02))) / 100,
        )

    @classmethod
    def legacy(cls, s: dict) -> "ExitConfig":
        """Eski sistem: kilit/kısmi yok, swing trailing ilk 4s kapanıştan itibaren."""
        return cls(trail_mode="swing", trail_activate_r=-math.inf, trail_swing_n=int(s.get("trend_exit_n", 10)),
                   trigger_source="candle_close", manage_tf="4h", lock_fee_cover=False)


@dataclass
class ExitState:
    side: str
    entry: float
    one_r: float                 # sabit 1R (fiyat farkı)
    stop: float                  # geçerli stop
    stop_initial: float
    best_r: float = 0.0          # tetik kaynağına göre görülen en iyi R
    lock_r: float | None = None  # ulaşılan en yüksek kilit seviyesi (R)
    partials_done: list = field(default_factory=list)
    trail_active: bool = False
    stop_source: str = "initial"

    @classmethod
    def new(cls, side: str, entry: float, stop: float) -> "ExitState":
        one_r = abs(entry - stop)
        if one_r <= 0:
            raise ValueError("1R sıfır olamaz")
        return cls(side=side, entry=entry, one_r=one_r, stop=stop, stop_initial=stop)

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1

    def to_dict(self) -> dict:
        x = asdict(self)
        if x["best_r"] == -math.inf:
            x["best_r"] = None
        return x

    @classmethod
    def from_dict(cls, x: dict) -> "ExitState":
        x = dict(x)
        x["best_r"] = x.get("best_r") or 0.0
        x["partials_done"] = list(x.get("partials_done") or [])
        return cls(**{k: x[k] for k in cls.__dataclass_fields__ if k in x})

    def r_at(self, price: float) -> float:
        return (price - self.entry) * self.d / self.one_r

    def price_at(self, r: float) -> float:
        return self.entry + self.d * r * self.one_r


def more_protective(side: str, a: float, b: float) -> bool:
    """a, b'den daha koruyucu mu? (LONG: daha yüksek, SHORT: daha düşük)"""
    return a > b if side == "LONG" else a < b


def round_stop(side: str, price: float, tick: Decimal | float | None) -> float:
    """Stopu tick'e KORUMA yönünde yuvarla (LONG yukarı, SHORT aşağı): yuvarlama stopu asla geri götürmez."""
    if not tick:
        return price
    t = Decimal(str(tick))
    q = (Decimal(str(price)) / t).quantize(Decimal(1), rounding=ROUND_CEILING if side == "LONG" else ROUND_FLOOR)
    return float(q * t)


def lock_price(st: ExitState, cfg: ExitConfig, lock_r: float) -> float:
    p = st.price_at(lock_r)
    if cfg.lock_fee_cover:
        p += st.d * abs(st.entry) * cfg.cost_frac
    return p


@dataclass
class Plan:
    partials: list = field(default_factory=list)   # [(kademe_indeksi, oran)]
    stop: float | None = None                       # yeni (daha koruyucu) stop
    stop_source: str = ""
    exit_now: bool = False                          # yeni stop fiyatın ötesinde: hemen kapat
    notes: list = field(default_factory=list)


def on_trigger(st: ExitState, cfg: ExitConfig, price: float, tick=None) -> Plan:
    """Tetik fiyatı (mark/last tik ya da mum kapanışı) geldiğinde: en iyi R'yi güncelle, kısmi TP ve kilit kararları.
    Durumu (best_r, lock_r, trail_active) GÜNCELLER; partials_done'u çağıran, emir başarılı olunca işaretler."""
    plan = Plan()
    r = st.r_at(price)
    if r > st.best_r:
        st.best_r = r
    for i, (trig, fr) in enumerate(cfg.partial_tiers):
        if i not in st.partials_done and st.best_r >= trig:
            plan.partials.append((i, fr))
    cands: dict[str, float] = {}
    reached = [lk for trig, lk in cfg.lock_tiers if st.best_r >= trig]
    if reached:
        lk = max(reached)
        if st.lock_r is None or lk > st.lock_r:
            st.lock_r = lk
        cands[f"kâr kilidi {st.lock_r:+.2f}R"] = lock_price(st, cfg, st.lock_r)
    if not st.trail_active and cfg.trail_mode != "off" and st.best_r >= cfg.trail_activate_r:
        st.trail_active = True
        plan.notes.append(f"trailing aktif (+{st.best_r:.2f}R görüldü)")
    new, src = choose_stop(st, cands, tick)
    if new is not None:
        plan.stop, plan.stop_source = new, src
        plan.exit_now = not more_protective(st.side, price, new)  # fiyat zaten yeni stopun ötesinde
    return plan


def trail_candidates(st: ExitState, cfg: ExitConfig, ref_close: float, atr_val: float | None,
                     lows, highs) -> dict[str, float]:
    """Mum kapanışında trailing adayları. lows/highs: son KAPANMIŞ 4s mumlar (look-ahead yok)."""
    out: dict[str, float] = {}
    if not st.trail_active or cfg.trail_mode == "off":
        return out
    if cfg.trail_mode in ("atr", "both") and atr_val and math.isfinite(atr_val) and atr_val > 0:
        out[f"ATR trailing {cfg.trail_atr_mult:g}×"] = ref_close - st.d * cfg.trail_atr_mult * atr_val
    n = cfg.trail_swing_n
    if cfg.trail_mode in ("swing", "both") and len(lows) >= n:
        out[f"swing trailing {n} mum"] = float(min(lows[-n:])) if st.side == "LONG" else float(max(highs[-n:]))
    return out


def choose_stop(st: ExitState, cands: dict[str, float], tick=None) -> tuple[float | None, str]:
    """Adaylar arasından en koruyucuyu seç; yuvarla; sadece mevcut stoptan DAHA koruyucuysa döndür."""
    best, src = st.stop, ""
    for name, p in cands.items():
        if p is None or not math.isfinite(p):
            continue
        p = round_stop(st.side, p, tick)
        if more_protective(st.side, p, best):
            best, src = p, name
    return (best, src) if src else (None, "")


def apply_stop(st: ExitState, new: float, source: str) -> None:
    """Stop değişikliğini duruma işler (geri gitmeyi kesinlikle reddeder)."""
    if not more_protective(st.side, new, st.stop):
        raise ValueError(f"Stop geri gidemez: {st.stop} -> {new}")
    st.stop = new
    st.stop_source = source
