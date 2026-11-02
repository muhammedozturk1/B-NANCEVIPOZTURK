"""SMC / ICT yapı analizi.

Kavramlar:
- Swing (fraktal) tepe/dip: likidite havuzlarının yeri (stoplar burada birikir).
- Likidite avı (sweep): fiyat bir dip/tepe havuzunun altına/üstüne iğne atıp geri döner.
- MSS (market structure shift): avdan sonra son ara tepenin gövde kapanışıyla kırılması.
- Displacement: kırılımı yapan güçlü, büyük gövdeli hareket.
- FVG (fair value gap): 3 mumluk boşluk; giriş CE'den (boşluğun ortası) yapılır.
- Order block: hareketten önceki son ters mum (FVG yoksa yedek giriş bölgesi).
- Premium/discount: işlem aralığının üst/alt yarısı. Long sadece ucuz bölgede tercih edilir.

Short kurulumlar için ayrı kod yazılmaz: mumlar aynalanır (fiyat -> -fiyat) ve
aynı long dedektörü çalıştırılır. Böylece long ve short mantığı birebir simetrik olur.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NY = ZoneInfo("America/New_York")
DAY_MS = 86_400_000


@dataclass
class Candles:
    time: np.ndarray        # açılış zamanı (ms)
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    taker_buy: np.ndarray
    interval_ms: int
    mirrored: bool = False

    @classmethod
    def from_cols(cls, cols: dict, interval_ms: int) -> "Candles":
        f = lambda k: np.asarray(cols[k], dtype=float)  # noqa: E731
        return cls(np.asarray(cols["time"], dtype=np.int64), f("open"), f("high"), f("low"), f("close"),
                   f("volume"), f("taker_buy"), interval_ms)

    def __len__(self) -> int:
        return len(self.close)

    @property
    def delta(self) -> np.ndarray:
        """Mum başına agresif alım - agresif satım hacmi (CVD'nin yapı taşı)."""
        return 2.0 * self.taker_buy - self.volume

    def mirror(self) -> "Candles":
        return Candles(self.time, -self.open, -self.low, -self.high, -self.close,
                       self.volume, self.volume - self.taker_buy, self.interval_ms, not self.mirrored)

    def slice(self, end: int) -> "Candles":
        return Candles(self.time[:end], self.open[:end], self.high[:end], self.low[:end], self.close[:end],
                       self.volume[:end], self.taker_buy[:end], self.interval_ms, self.mirrored)


def atr(c: Candles, n: int = 14) -> np.ndarray:
    h, l, cl = c.high, c.low, c.close
    prev = np.concatenate([[cl[0]], cl[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def pivots(c: Candles, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Fraktal tepe ve dipler. i indisli pivot, i+n mumu kapanınca kesinleşir."""
    if len(c) < 2 * n + 1:
        return np.array([], dtype=int), np.array([], dtype=int)
    wh = sliding_window_view(c.high, 2 * n + 1)
    wl = sliding_window_view(c.low, 2 * n + 1)
    ch, cl = wh[:, n], wl[:, n]
    sh = (ch > wh[:, :n].max(1)) & (ch >= wh[:, n + 1:].max(1))
    sl = (cl < wl[:, :n].min(1)) & (cl <= wl[:, n + 1:].min(1))
    return np.nonzero(sh)[0] + n, np.nonzero(sl)[0] + n


class Analysis:
    """Bir mum serisinin önceden hesaplanmış analizi (canlıda ve geçmiş testte ortak)."""

    def __init__(self, c: Candles, swing_n: int = 3):
        self.c = c
        self.n = swing_n
        self.atr = atr(c)
        self.sh, self.sl = pivots(c, swing_n)
        self.cvd = np.cumsum(c.delta)
        self._daily_levels()

    def _daily_levels(self) -> None:
        """Önceki gün tepe/dibi (PDH/PDL) ve bugünün o ana kadarki tepe/dibi."""
        c = self.c
        L = len(c)
        day = (c.time // DAY_MS).astype(np.int64)
        self.pdh = np.full(L, np.nan)
        self.pdl = np.full(L, np.nan)
        self.today_high_before = np.full(L, np.nan)   # bugünün t'ye kadarki (t hariç) tepesi
        self.today_low_before = np.full(L, np.nan)
        if L == 0:
            return
        days = np.unique(day)
        day_hi = {int(d): c.high[day == d].max() for d in days}
        day_lo = {int(d): c.low[day == d].min() for d in days}
        # Serinin ilk günü yarım olabilir; yalnızca tam günleri PDH/PDL say
        complete = {int(d) for d in days[1:]}
        if c.time[0] % DAY_MS == 0:
            complete.add(int(days[0]))
        run_hi, run_lo, cur = -np.inf, np.inf, None
        for i in range(L):
            d = int(day[i])
            if d != cur:
                cur, run_hi, run_lo = d, -np.inf, np.inf
            if (d - 1) in complete:
                self.pdh[i] = day_hi[d - 1]
                self.pdl[i] = day_lo[d - 1]
            self.today_high_before[i] = run_hi if np.isfinite(run_hi) else np.nan
            self.today_low_before[i] = run_lo if np.isfinite(run_lo) else np.nan
            run_hi = max(run_hi, c.high[i])
            run_lo = min(run_lo, c.low[i])


class HTFContext:
    """Üst zaman dilimi: trend yönü (BOS) ve işlem aralığı (premium/discount)."""

    RANGE_BARS = 48

    def __init__(self, c: Candles, swing_n: int = 3):
        self.c = c
        self.n = swing_n
        self.sh, self.sl = pivots(c, swing_n)
        L = len(c)
        self.bias = np.zeros(L, dtype=int)
        self.range_hi = np.full(L, np.nan)
        self.range_lo = np.full(L, np.nan)
        sh_conf = {int(i) + swing_n: int(i) for i in self.sh}
        sl_conf = {int(i) + swing_n: int(i) for i in self.sl}
        last_sh = last_sl = np.nan
        b = 0
        for i in range(L):
            if i in sh_conf:
                last_sh = c.high[sh_conf[i]]
            if i in sl_conf:
                last_sl = c.low[sl_conf[i]]
            if np.isfinite(last_sh) and c.close[i] > last_sh:
                b = 1
            elif np.isfinite(last_sl) and c.close[i] < last_sl:
                b = -1
            self.bias[i] = b
            lo = max(0, i - self.RANGE_BARS + 1)
            self.range_hi[i] = c.high[lo:i + 1].max()
            self.range_lo[i] = c.low[lo:i + 1].min()

    def index_for(self, ltf_close_time: int) -> int:
        """LTF mumu kapandığında kapanmış son HTF mumunun indisi."""
        closes = self.c.time + self.c.interval_ms
        return int(np.searchsorted(closes, ltf_close_time, side="right")) - 1

    def position_in_range(self, j: int, price: float) -> float:
        hi, lo = self.range_hi[j], self.range_lo[j]
        if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
            return 0.5
        return float((price - lo) / (hi - lo))

    def unswept_highs(self, j: int, above: float, lookback: int = 120) -> list[float]:
        c = self.c
        out = []
        for q in self.sh[(self.sh >= j - lookback) & (self.sh + self.n <= j)]:
            lvl = c.high[q]
            if lvl > above and (q + 1 > j or c.high[q + 1:j + 1].max() <= lvl):
                out.append(float(lvl))
        return out


def in_killzone(open_time_ms: int, interval_ms: int) -> str | None:
    """ICT killzone'ları New York saatine göre (yaz/kış saati otomatik)."""
    t = datetime.fromtimestamp((open_time_ms + interval_ms) / 1000, tz=timezone.utc).astimezone(NY)
    h = t.hour + t.minute / 60
    if 2 <= h < 5:
        return "Londra killzone"
    if 7 <= h < 10:
        return "New York killzone"
    return None


# ----------------------------------------------------------------------------
# Long kurulum dedektörü (short için aynalanmış seride çalıştırılır)
# ----------------------------------------------------------------------------
@dataclass
class RawSetup:
    t: int
    s: int                  # avın yapıldığı mum
    pool_idx: int           # süpürülen havuzun mumu (-1: PDL gibi)
    pool_level: float
    pool_kind: str          # equal | pdl | swing
    wick_sweep: bool
    q: int                  # MSS seviyesini oluşturan ara tepe
    mss_level: float
    displacement: float     # en büyük gövde / ATR
    zone_type: str          # FVG | OB
    zone_top: float
    zone_bottom: float
    zone_time_idx: int
    entry: float
    stop: float
    atr: float
    leg_high: float
    retrace: float          # girişin bacaktaki geri çekilme oranı (0.5+ = discount)
    cvd_div: bool
    sweep_delta_pos: bool
    leg_delta: float


SWEEP_LOOKBACK = 24
MAX_SWEEP_TO_MSS = 16


def _swept_pool(A: Analysis, s: int, atr_v: float) -> tuple[int, float, str] | None:
    c = A.c
    ls = c.low[s]
    tol = 0.15 * atr_v
    max_depth = 1.5 * atr_v
    cands: list[tuple[int, float, str]] = []

    lows = A.sl[(A.sl + A.n < s) & (A.sl >= s - 150)]
    intact = []
    for p in lows:
        lvl = c.low[p]
        if not (ls < lvl <= ls + max_depth):
            continue
        if p + 1 < s and c.low[p + 1:s].min() < lvl:
            continue  # daha önce süpürülmüş, havuz boş
        intact.append(int(p))
    for p in intact:
        lvl = c.low[p]
        # Eşit dipler: aynı seviyede ikinci bir dip -> daha çok stop birikmiş, daha güçlü havuz
        equal = any(int(p2) != p and abs(c.low[p2] - lvl) <= tol for p2 in lows)
        cands.append((p, float(lvl), "equal" if equal else "swing"))

    pdl = A.pdl[s]
    tlb = A.today_low_before[s]
    if np.isfinite(pdl) and ls < pdl <= ls + max_depth and (not np.isfinite(tlb) or tlb > pdl):
        cands.append((-1, float(pdl), "pdl"))

    if not cands:
        return None
    rank = {"equal": 3, "pdl": 2, "swing": 1}
    cands.sort(key=lambda x: (rank[x[2]], x[0]), reverse=True)
    return cands[0]


def find_long(A: Analysis, t: int, min_stop_pct: float, max_stop_pct: float, entry_mode: str = "ce") -> RawSetup | None:
    c = A.c
    if t < 60 or t >= len(c):
        return None
    atr_v = A.atr[t]
    if not np.isfinite(atr_v) or atr_v <= 0:
        return None

    lo = max(0, t - SWEEP_LOOKBACK)
    s = lo + int(np.argmin(c.low[lo:t]))
    if not (1 <= t - s <= MAX_SWEEP_TO_MSS):
        return None
    if c.low[t] <= c.low[s]:
        return None

    # MSS: avdan önceki son ara tepe, t mumunda İLK kez gövdeyle kırılmalı
    cand = A.sh[(A.sh < s) & (A.sh + A.n <= t) & (A.sh >= s - 60)]
    if len(cand) == 0:
        return None
    q = int(cand[-1])
    mss = c.high[q]
    if not c.close[t] > mss:
        return None
    if (c.close[s:t] > mss).any():
        return None

    pool = _swept_pool(A, s, atr_v)
    if pool is None:
        return None
    pool_idx, pool_level, pool_kind = pool
    if c.close[s:min(s + 3, t + 1)].max() <= pool_level:
        return None  # geri alınmadı: av değil kırılım
    wick = bool(c.close[s] > pool_level)

    bodies = np.abs(c.close[s + 1:t + 1] - c.open[s + 1:t + 1])
    disp = float(bodies.max() / atr_v) if len(bodies) else 0.0
    mss_body = (c.close[t] - c.open[t]) / atr_v
    if disp < 0.8 and mss_body < 0.6:
        return None  # zayıf kırılım

    stop = c.low[s] - 0.1 * atr_v
    leg_high = float(c.high[s:t + 1].max())

    zone = None
    for i in range(t, s + 1, -1):
        if i - 2 < s:
            break
        if c.low[i] > c.high[i - 2] and c.close[i - 1] > c.open[i - 1]:
            bottom, top = c.high[i - 2], c.low[i]
            if top - bottom < 0.05 * atr_v:
                continue
            ce = (top + bottom) / 2
            if i < t and c.low[i + 1:t + 1].min() <= ce:
                continue
            ent = top if entry_mode == "edge" else ce
            if entry_mode != "market" and (ent >= c.close[t] or ent <= stop):
                continue
            zone = ("FVG", float(top), float(bottom), i - 2, float(ent))
            break
    if zone is None:
        for j in range(s, max(-1, s - 4), -1):
            if c.close[j] < c.open[j]:
                top, bottom = c.open[j], c.low[j]
                entry = top if entry_mode == "edge" else (c.open[j] + c.close[j]) / 2
                if entry_mode == "market" or stop < entry < c.close[t]:
                    zone = ("OB", float(top), float(bottom), j, float(entry))
                break
    if zone is None:
        return None
    ztype, ztop, zbot, zidx, entry = zone
    if entry_mode == "market":
        entry = float(c.close[t])  # MSS kapanışında doğrudan giriş (geri çekilme beklenmez)

    risk = entry - stop
    if risk <= 0:
        return None
    stop_pct = abs(risk / entry) * 100
    if not (min_stop_pct <= stop_pct <= max_stop_pct):
        return None

    leg = leg_high - c.low[s]
    retrace = float((leg_high - entry) / leg) if leg > 0 else 0.0

    cvd_div = False
    if pool_idx >= 0:
        cvd_div = bool(c.low[s] < c.low[pool_idx] and A.cvd[s] > A.cvd[pool_idx])
    sweep_delta_pos = bool(c.delta[s] > 0)
    leg_delta = float(c.delta[s + 1:t + 1].sum())

    return RawSetup(t=t, s=s, pool_idx=pool_idx, pool_level=pool_level, pool_kind=pool_kind, wick_sweep=wick,
                    q=q, mss_level=float(mss), displacement=max(disp, float(mss_body)), zone_type=ztype,
                    zone_top=ztop, zone_bottom=zbot, zone_time_idx=zidx, entry=float(entry), stop=float(stop),
                    atr=float(atr_v), leg_high=leg_high, retrace=retrace, cvd_div=cvd_div,
                    sweep_delta_pos=sweep_delta_pos, leg_delta=leg_delta)


def liquidity_targets(A: Analysis, t: int, above: float, lookback: int = 200) -> list[tuple[float, str, int]]:
    """Girişin üstündeki süpürülmemiş likidite (hedefler). (fiyat, tür, kalite)."""
    c = A.c
    out: list[tuple[float, str, int]] = []
    highs = [int(q) for q in A.sh[(A.sh >= t - lookback) & (A.sh + A.n <= t)]]
    unswept = [q for q in highs if c.high[q] > above and (q + 1 > t or c.high[q + 1:t + 1].max() <= c.high[q])]
    tol = 0.15 * A.atr[t]
    used = set()
    for q in unswept:
        if q in used:
            continue
        group = [q2 for q2 in unswept if abs(c.high[q2] - c.high[q]) <= tol]
        used.update(group)
        lvl = float(min(c.high[g] for g in group))  # eşit tepelerde en alttaki seviye ilk dolar
        out.append((lvl, "equal" if len(group) > 1 else "swing", 2 if len(group) > 1 else 1))
    pdh = A.pdh[t]
    thb = A.today_high_before[t]
    if np.isfinite(pdh) and pdh > above and (not np.isfinite(thb) or max(thb, c.high[t]) < pdh):
        out.append((float(pdh), "pdh", 2))
    out.sort(key=lambda x: x[0])
    return out
