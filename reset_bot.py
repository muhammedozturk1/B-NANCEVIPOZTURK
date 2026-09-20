"""
BOT SIFIRLAMA SCRIPTİ

Bu script:
  1) Borsadaki açık pozisyonları (varsa) kapatır
  2) Tüm işlem geçmişini, motor durumlarını, günlük PnL kayıtlarını siler
  3) Sanal kasayı STARTING_BALANCE'a (varsayılan $100) sıfırlar

DİKKAT: Bu işlem GERİ ALINAMAZ. Sadece testnet'te / bilinçli bir sıfırlama
istediğinde kullan.
"""
from backend.exchange.binance_client import BinanceClient
from backend.db.database import get_session
from backend.db.models import Trade, EngineState, DailyPnL, BotCapital

print("=== BOT SIFIRLAMA BAŞLIYOR ===")

# 1) Açık pozisyonları kapat
client = BinanceClient()
positions = client.fetch_open_positions()

if not positions:
    print("Açık pozisyon yok, devam ediliyor.")
else:
    for p in positions:
        symbol = p["symbol"]
        side = "buy" if p["side"] == "long" else "sell"
        amount = abs(float(p["contracts"]))
        print(f"Kapatılıyor: {symbol} {side} {amount}")
        try:
            client.cancel_all_algo_orders(symbol)
        except Exception as e:
            print("Algo emir iptal hatası (önemli değil):", e)
        try:
            client.close_position(symbol, side, amount)
            print(f"✅ Kapandı: {symbol}")
        except Exception as e:
            print(f"❌ {symbol} kapatılamadı: {e}")

# 2) Veritabanını temizle
session = get_session()
try:
    deleted_trades = session.query(Trade).delete()
    deleted_states = session.query(EngineState).delete()
    deleted_pnl = session.query(DailyPnL).delete()
    deleted_capital = session.query(BotCapital).delete()
    session.commit()
    print(f"Silindi -> Trade: {deleted_trades}, EngineState: {deleted_states}, "
          f"DailyPnL: {deleted_pnl}, BotCapital: {deleted_capital}")
finally:
    session.close()

print("=== SIFIRLAMA TAMAMLANDI - bot bir sonraki döngüde $100 sanal kasayla baştan başlayacak ===")
