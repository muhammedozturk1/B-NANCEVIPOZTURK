"""
Binance Futures bağlantı katmanı.
Testnet / canlı geçişi tek bir config değişkeni (USE_TESTNET) ile yapılır.
Tüm borsa işlemleri bu sınıf üzerinden geçer -> ileride başka borsa eklemek
istersek sadece bu dosyayı değiştiririz, strateji kodlarına dokunmayız.
"""
import time
import hmac
import hashlib
import requests
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
            self.base_url = "https://testnet.binancefuture.com"
            logger.info("Binance TESTNET modunda başlatıldı.")
        else:
            self.base_url = "https://fapi.binance.com"
            logger.warning("Binance CANLI modda başlatıldı - gerçek para kullanılıyor!")

        self.exchange.load_markets()  # algo order isteklerinde sembol dönüşümü (BTC/USDT -> BTCUSDT) için gerekli

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
    # ALGO ORDER API (SL/TP) - Binance'in Aralık 2025'te zorunlu kıldığı yeni sistem
    # ------------------------------------------------------------------
    # ÖNEMLİ: Binance, Aralık 2025'ten itibaren STOP_MARKET/TAKE_PROFIT_MARKET gibi
    # koşullu emirleri eski /fapi/v1/order endpoint'inden KABUL ETMİYOR (hata -4120).
    # Bu emirler artık ayrı bir "Algo Order" servisi (/fapi/v1/algoOrder) üzerinden
    # gönderilmeli. ccxt'nin unified create_order() metodu henüz bunu desteklemediği
    # için, bu emirleri DOĞRUDAN (ccxt'yi atlayarak) imzalı HTTP isteğiyle gönderiyoruz.
    def _signed_algo_request(self, method: str, path: str, params: dict):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            config.BINANCE_API_SECRET.encode(), query_string.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
        url = f"{self.base_url}{path}"

        response = requests.request(method, url, params=params, headers=headers, timeout=10)
        if not response.ok:
            logger.error(f"Algo order isteği başarısız ({response.status_code}): {response.text}")
        response.raise_for_status()
        return response.json()

    def _place_algo_stop_order(self, symbol: str, entry_side: str, amount: float,
                                trigger_price: float, order_type: str):
        """order_type: 'STOP_MARKET' veya 'TAKE_PROFIT_MARKET'."""
        close_side = "SELL" if entry_side == "buy" else "BUY"
        market_symbol = self.exchange.market(symbol)["id"]  # 'BTC/USDT' -> 'BTCUSDT'

        params = {
            "algoType": "CONDITIONAL",
            "symbol": market_symbol,
            "side": close_side,
            "type": order_type,
            "triggerPrice": trigger_price,
            "quantity": amount,
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        return self._signed_algo_request("POST", "/fapi/v1/algoOrder", params)

    def get_open_algo_orders(self, symbol: str) -> list:
        market_symbol = self.exchange.market(symbol)["id"]
        result = self._signed_algo_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": market_symbol})
        return result if isinstance(result, list) else result.get("data", [])

    def cancel_algo_order(self, algo_id):
        return self._signed_algo_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})

    def cancel_open_stop_orders(self, symbol: str):
        """Bu sembol için açık tüm SL/TP (algo) emirlerini iptal eder."""
        try:
            orders = self.get_open_algo_orders(symbol)
            for order in orders:
                self.cancel_algo_order(order["algoId"])
        except Exception as e:
            logger.error(f"{symbol}: açık algo emirleri iptal edilemedi: {e}")

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
            try:
                self._place_algo_stop_order(symbol, side, amount, stop_loss, order_type="STOP_MARKET")
            except Exception as e:
                logger.error(f"❌ {symbol}: Stop-loss emri KONULAMADI, pozisyon korumasız! Hata: {e}")
        if take_profit:
            try:
                self._place_algo_stop_order(symbol, side, amount, take_profit, order_type="TAKE_PROFIT_MARKET")
            except Exception as e:
                logger.error(f"❌ {symbol}: Take-profit emri konulamadı: {e}")

        return order

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
        """Breakeven / trailing stop için mevcut SL/TP algo emirlerini iptal edip yeni SL koyar."""
        self.cancel_open_stop_orders(symbol)
        return self._place_algo_stop_order(symbol, side, amount, new_stop_price, order_type="STOP_MARKET")

    # ------------------------------------------------------------------
    # DİNAMİK PİYASA TARAMASI
    # ------------------------------------------------------------------
    def fetch_top_symbols(self, limit: int, min_volume: float, quote: str = "USDT",
                           exclude_base_assets: set = None) -> list:
        """
        Binance Futures'taki tüm <quote> paritelerini 24s hacme göre sıralar,
        minimum hacim ve dışlanan bazları (stablecoin'ler vb.) filtreleyip
        en aktif <limit> tanesini döner. Sabit bir coin listesi YOKTUR -
        piyasa hangi coin'lerde hareketliyse bot oraya bakar.
        """
        exclude_base_assets = exclude_base_assets or set()
        tickers = self.exchange.fetch_tickers()

        candidates = []
        seen = set()
        for raw_symbol, ticker in tickers.items():
            base_symbol = raw_symbol.split(":")[0]  # 'BTC/USDT:USDT' -> 'BTC/USDT'
            if not base_symbol.endswith(f"/{quote}") or base_symbol in seen:
                continue

            base_asset = base_symbol.split("/")[0]
            if base_asset in exclude_base_assets:
                continue

            volume = ticker.get("quoteVolume") or 0
            if volume < min_volume:
                continue

            seen.add(base_symbol)
            candidates.append((base_symbol, volume))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return [symbol for symbol, _ in candidates[:limit]]

    # ------------------------------------------------------------------
    # SENKRONİZASYON (bağlantı koptuktan sonra state kurtarma)
    # ------------------------------------------------------------------
    def fetch_open_positions(self):
        """Borsadaki gerçek açık pozisyonları döner - reconnect sonrası DB ile
        karşılaştırılıp 'yetim' pozisyon kalmaması sağlanır."""
        positions = self.exchange.fetch_positions()
        return [p for p in positions if float(p.get("contracts", 0)) != 0]
