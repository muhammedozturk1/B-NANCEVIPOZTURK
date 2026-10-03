"""
Fonlama stratejisinin karar kuralları - SAF fonksiyonlar.
Hem canlı (paper) motor hem backtest BİREBİR aynı kuralları kullanır.

Fonlama oranı: vadeli kontratta long ve short'lar arasında her 1/4/8 saatte bir
yapılan ödeme. Oran POZİTİFSE long'lar short'lara öder. Bizim pozisyonumuz
"spot long + vadeli short" olduğu için pozitif oranlarda para TOPLARIZ, fiyat
değişimi ise iki bacakta birbirini götürür.
"""
import numpy as np
from backend import config

HOUR_MS = 3_600_000


def cfg():
    return config.FUNDING


def round_trip_cost() -> float:
    """Giriş + çıkış, iki bacak: nominalin oranı olarak toplam maliyet."""
    c = cfg()
    return 2 * (c["spot_fee"] + c["slippage"]) + 2 * (c["perp_fee"] + c["slippage"])


def interval_hours(timestamps_ms) -> float:
    ts = sorted(timestamps_ms)
    if len(ts) < 2:
        return 8.0
    med = float(np.median(np.diff(ts))) / HOUR_MS
    return med if med > 0.5 else 8.0


def to_apr(rate: float, interval_h: float) -> float:
    return rate * (24.0 / interval_h) * 365.0


class Series:
    """Bir coinin fonlama geçmişi (sıralı numpy dizileri + kümülatif toplam) - hızlı pencere sorguları."""
    def __init__(self, events):
        ev = sorted(events)
        self.ts = np.array([t for t, _ in ev], dtype=np.int64)
        self.rates = np.array([r for _, r in ev], dtype=float)
        self.cs = np.concatenate([[0.0], np.cumsum(self.rates)])

    def sum_between(self, after_ms: int, until_ms: int) -> tuple:
        """(after, until] aralığındaki oranların toplamı ve adedi."""
        i0 = np.searchsorted(self.ts, after_ms, "right")
        i1 = np.searchsorted(self.ts, until_ms, "right")
        return float(self.cs[i1] - self.cs[i0]), int(i1 - i0)


def series_stats(series: "Series", now_ms: int, lookback_h: float = None) -> dict:
    lookback_h = lookback_h or cfg()["lookback_hours"]
    i1 = int(np.searchsorted(series.ts, now_ms, "right"))
    i0 = int(np.searchsorted(series.ts, now_ms - lookback_h * HOUR_MS, "right"))
    n = i1 - i0
    if n <= 0:
        return {"n": 0, "expected_n": 1, "avg_apr": 0.0, "last_apr": 0.0, "min_rate": 0.0,
                "interval_h": 8.0, "last_rates": []}
    ih = interval_hours(series.ts[max(0, i1 - 30):i1].tolist())
    window = series.rates[i0:i1]
    return {
        "n": n,
        "expected_n": lookback_h / ih,
        "avg_apr": to_apr(float((series.cs[i1] - series.cs[i0]) / n), ih),
        "last_apr": to_apr(float(window[-1]), ih),
        "min_rate": float(window.min()),
        "interval_h": ih,
        "last_rates": window[-3:].tolist(),
    }


def trailing_stats(events, now_ms: int, lookback_h: float = None) -> dict:
    """events: [(ts_ms, rate), ...] -> son lookback saatin istatistikleri (canlı motor)."""
    return series_stats(Series(events), now_ms, lookback_h)


def entry_ok(stats: dict) -> bool:
    c = cfg()
    return (
        stats["n"] > 0
        and stats["n"] >= 0.8 * stats.get("expected_n", stats["n"])   # yeterli geçmiş
        and stats["avg_apr"] >= c["entry_min_apr"]
        and stats["last_apr"] >= c["entry_last_min_apr"]
        and stats["min_rate"] >= 0                                      # son 3 günde hiç negatif yok
    )


def exit_reason(stats: dict, price_move: float, held_hours: float):
    """Çıkış gerekiyorsa sebebini, yoksa None döner."""
    c = cfg()
    if abs(price_move) >= c["max_price_move"]:
        return "fiyat_koruma"
    strongly_negative = stats["n"] > 0 and stats["avg_apr"] < -0.10
    if held_hours < c["min_hold_hours"]:
        return "negatif_fonlama" if strongly_negative else None
    if stats["n"] > 0 and stats["avg_apr"] < c["exit_apr"]:
        return "fonlama_dustu"
    if len(stats["last_rates"]) >= 2 and all(r < 0 for r in stats["last_rates"][-2:]):
        return "negatif_fonlama"
    return None
