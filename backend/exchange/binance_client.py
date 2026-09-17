"""
Binance Futures bağlantı katmanı.
Testnet / canlı geçişi tek bir config değişkeni (USE_TESTNET) ile yapılır.
Tüm borsa işlemleri bu sınıf üzerinden geçer -> ileride başka borsa eklemek
istersek sadece bu dosyayı değiştiririz, strateji kodlarına dokunmayız.
"""
import ccxt
import logging
from backend import config

logger = logging.getLogger("binance_client")


class BinanceClient:
    def __init__(self):
        self.exchange = ccxt.binance({
            "apiKey": config.BINANCE_API_KEY,
            "secret": config.BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })

        if config.USE_TESTNET:
            self.exchange.set_sandbox_mode(True)
            logger.info("Binance TESTNET modunda başlatıldı.")
        else:
            logger.warning("Binance CANLI modda başlatıldı - gerçek para kullanılıyor!")

    # ------------------------------------------------------------------
    # PİYASA VERİSİ
    # ------------------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200):
        """Mum verisi çeker: [timestamp, open, high, low, close, volume]"""
        return self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_order_book(self, symbol: str, limit: int = 100):
        return self.exchange.fetch_order_book(symbol, limit=limit)

    def fetch_ticker(self, symbol: str):
        return self.exchange.fetch_ticker(symbol)

    # ------------------------------------------------------------------
    # HESAP / BAKİYE
    # ------------------------------------------------------------------
    def fetch_balance(self):
        return self.exchange.fetch_balance()

    def get_usdt_balance(self) -> float:
        balance = self.fetch_balance()
        return float(balance.get("USDT", {}).get("free", 0))

    # ------------------------------------------------------------------
    # KALDIRAÇ AYARI
    # ------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.error(f"Kaldıraç ayarlanamadı ({symbol}, {leverage}x): {e}")

    # ------------------------------------------------------------------
    # POZİSYON AÇMA / KAPAMA
    # ------------------------------------------------------------------
    def open_position(self, symbol: str, side: str, amount: float,
                       stop_loss: float = None, take_profit: float = None):
        """
        side: 'buy' (long) veya 'sell' (short)
        amount: kontrat/coin miktarı (pozisyon büyüklüğü hesaplanmış olarak gelir)
        """
        order = self.exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
        )
        logger.info(f"Pozisyon açıldı: {symbol} {side} {amount}")

        if stop_loss:
            self._place_stop_order(symbol, side, amount, stop_loss, order_type="stop_market")
        if take_profit:
            self._place_stop_order(symbol, side, amount, take_profit, order_type="take_profit_market")

        return order

    def _place_stop_order(self, symbol: str, entry_side: str, amount: float,
                           trigger_price: float, order_type: str):
        # SL/TP emirleri pozisyonun tersi yönde kapanış emri olarak girilir
        close_side = "sell" if entry_side == "buy" else "buy"
        return self.exchange.create_order(
            symbol=symbol,
            type=order_type,
            side=close_side,
            amount=amount,
            params={"stopPrice": trigger_price, "reduceOnly": True},
        )

    def close_position(self, symbol: str, side: str, amount: float):
        close_side = "sell" if side == "buy" else "buy"
        return self.exchange.create_order(
            symbol=symbol,
            type="market",
            side=close_side,
            amount=amount,
            params={"reduceOnly": True},
        )

    def update_stop_loss(self, symbol: str, side: str, amount: float, new_stop_price: float):
        """Breakeven / trailing stop için mevcut SL'yi iptal edip yenisini koyar."""
        self.cancel_open_stop_orders(symbol)
        return self._place_stop_order(symbol, side, amount, new_stop_price, order_type="stop_market")

    def cancel_open_stop_orders(self, symbol: str):
        open_orders = self.exchange.fetch_open_orders(symbol)
        for o in open_orders:
            if o["type"] in ("stop_market", "take_profit_market"):
                self.exchange.cancel_order(o["id"], symbol)

    # ------------------------------------------------------------------
    # SENKRONİZASYON (bağlantı koptuktan sonra state kurtarma)
    # ------------------------------------------------------------------
    def fetch_open_positions(self):
        """Borsadaki gerçek açık pozisyonları döner - reconnect sonrası DB ile
        karşılaştırılıp 'yetim' pozisyon kalmaması sağlanır."""
        positions = self.exchange.fetch_positions()
        return [p for p in positions if float(p.get("contracts", 0)) != 0]
