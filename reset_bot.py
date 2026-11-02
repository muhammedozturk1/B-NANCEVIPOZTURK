"""
BOT SIFIRLAMA SCRIPTİ

Bu script:
  1) Borsadaki açık pozisyonları (varsa) kapatır
  2) Tüm işlem geçmişini, motor durumlarını, günlük PnL kayıtlarını siler
  3) Sanal kasayı STARTING_BALANCE'a (varsayılan $100) sıfırlar

DİKKAT: Bu işlem GERİ ALINAMAZ. Sadece testnet'te / bilinçli bir sıfırlama
istediğinde kullan.

    python reset_bot.py          -> mevcut bot (yukarıdaki adımlar)
    python reset_bot.py --smc    -> ek olarak SMC Scalper'ın tüm kayıtlarını ve 72 saatlik
                                    trial'ını da siler (yeni bir 72 saat başlar)
"""
import sys
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

if "--smc" in sys.argv:
    from backend.smc.config import Settings
    from backend.smc.database.db import Database
    from backend.smc.database.models import Base as SmcBase
    smc_db = Database(Settings().database_url)
    SmcBase.metadata.drop_all(smc_db.engine)
    SmcBase.metadata.create_all(smc_db.engine)
    print("SMC kayıtları ve trial sıfırlandı (yeni 72 saat SMC'nin bir sonraki başlangıcında başlar).")
