"""
Pozisyon büyüklüğü, kaldıraç seçimi ve TP/SL fiyat hesaplama.
Bu dosya botun "para yönetimi" beynidir - tüm motorlar buradan geçer.
"""
import pandas as pd
from backend import config


def calculate_atr(ohlcv: list, period: int = 14) -> float:
    """Average True Range - volatilite ölçümü için kullanılır."""
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return float(atr.iloc[-1])


def calculate_stop_distance(entry_price: float, atr: float, multiplier: float) -> float:
    """
    Stop mesafesini ATR*çarpan olarak hesaplar, ancak MIN_STOP_DISTANCE_PERCENT
    tabanının altına düşmesine izin vermez. Bu taban özellikle düşük fiyatlı/
    oynak (meme coin tarzı) paritelerde ATR'nin yanıltıcı derecede küçük
    çıktığı durumlarda, işlemin açılır açılmaz anında TP/SL'e çarpmasını önler.
    """
    atr_based_distance = atr * multiplier
    min_distance = entry_price * config.MIN_STOP_DISTANCE_PERCENT
    return max(atr_based_distance, min_distance)


def choose_leverage(ohlcv: list) -> int:
    """ATR/fiyat oranına göre otomatik kaldıraç seçimi."""
    atr = calculate_atr(ohlcv)
    current_price = ohlcv[-1][4]
    volatility_ratio = atr / current_price

    if volatility_ratio < config.VOLATILITY_ATR_THRESHOLD:
        return config.LEVERAGE_LOW_VOLATILITY   # düşük volatilite -> 20x
    return config.LEVERAGE_HIGH_VOLATILITY       # yüksek volatilite -> 10x


def calculate_risk_amount(engine: str, current_balance: float) -> float:
    """İşlem başına risklenecek $ tutarı: max($5, kasa * risk%)"""
    percent_based = current_balance * config.RISK_PERCENT_PER_TRADE[engine]
    return max(config.MIN_RISK_USD, percent_based)


def calculate_position_size(engine: str, entry_price: float, stop_loss_price: float,
                             current_balance: float, leverage: int) -> dict:
    """
    Risk tutarını, giriş ve stop mesafesine bölerek pozisyon miktarını (coin adedi) hesaplar.
    Kaldıraç, işlem büyüklüğünü DEĞİL, gereken teminatı etkiler - risk her zaman sabit $ bazlıdır.
    Bu, "kaldıraç yüksek diye daha fazla risk almış olma" hatasını engeller.

    GÜVENLİK TAVANI: Eğer hesaplanan pozisyon, kasanın MAX_MARGIN_PERCENT_PER_TRADE'ini
    aşan bir marj istiyorsa (örn. dar stop mesafesi yüzünden), pozisyon büyüklüğü bu
    tavana sığacak şekilde otomatik küçültülür. Bu durumda gerçek risk, MIN_RISK_USD'nin
    altına da düşebilir - küçük kasada güvenlik, sabit risk tutarından önce gelir.
    """
    risk_usd = calculate_risk_amount(engine, current_balance)
    stop_distance = abs(entry_price - stop_loss_price)

    if stop_distance == 0:
        raise ValueError("Stop-loss mesafesi 0 olamaz")

    # Kaç coin alırsak, stop'a çarpınca tam olarak risk_usd kaybederiz
    position_amount = risk_usd / stop_distance
    position_value = position_amount * entry_price
    required_margin = position_value / leverage

    max_margin = current_balance * config.MAX_MARGIN_PERCENT_PER_TRADE
    capped = False

    if required_margin > max_margin and required_margin > 0:
        scale = max_margin / required_margin
        position_amount *= scale
        position_value *= scale
        risk_usd *= scale  # gerçek risk de orantılı küçülür
        required_margin = max_margin
        capped = True

    return {
        "risk_usd": round(risk_usd, 2),
        "amount": position_amount,
        "position_value_usd": round(position_value, 2),
        "required_margin_usd": round(required_margin, 2),
        "leverage": leverage,
        "capped_by_margin_limit": capped,
    }


def calculate_tp_prices(engine: str, entry_price: float, stop_loss_price: float, side: str) -> dict:
    """Motor bazlı R/R oranlarına göre TP1 ve TP2 fiyatlarını hesaplar."""
    stop_distance = abs(entry_price - stop_loss_price)
    rr = config.RISK_REWARD[engine]

    direction = 1 if side == "buy" else -1
    tp1 = entry_price + direction * stop_distance * rr["tp1"]
    tp2 = entry_price + direction * stop_distance * rr["tp2"]

    return {"take_profit_1": tp1, "take_profit_2": tp2}


def check_margin_sufficient(required_margin: float, available_balance: float) -> bool:
    """$100 gibi küçük kasada teminatın yetip yetmediğini kontrol eder."""
    return required_margin <= available_balance * 0.95  # %5 pay bırak
