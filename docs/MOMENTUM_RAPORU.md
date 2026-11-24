# Momentum Portföy Stratejisi — Geçmiş Test Raporu

## Strateji
**Coinler:** BTC, ETH, SOL, BNB, XRP, AVAX, LINK, DOGE. Panelde değiştirilebilir.

**Her rebalance'ta (varsayılan 8 saatte bir: UTC 00, 08, 16):**
1. Her coin için skor hesaplanır: 7, 14 ve 28 günlük (getiri / aynı penceredeki volatilite) değerlerinin ortalaması.
2. En yüksek skorlu %25 long, en düşük skorlu %25 short açılır. Ortada kalan coinler tutulmaz.
3. Filtreye takılan coinin yerine sıradaki coin geçmez. Filtreler:
   - 24 saatlik hacim alt sınırı
   - Funding: long'da 8 saatlik oran %0,03'ün üstündeyse, short'ta −%0,03'ün altındaysa açılmaz
4. **İşaret şartı** (varsayılan açık, şartnamede yok): long için skor pozitif, short için skor negatif olmalı.

**Pozisyon büyüklüğü**
1. Risk paritesi: her coin yaklaşık eşit volatilite taşır.
2. 30 günlük kovaryansla portföy volatilitesi hesaplanır.
3. Ölçek = %20 hedef volatilite / gerçekleşen volatilite.
4. Tavanlar:
   - Tek coinde nominal en fazla bakiyenin %15'i
   - Koruma stopunda kayıp en fazla bakiyenin %1,5'i (stop mesafesi = 3 × günlük volatilite)
   - Toplam kaldıraç en fazla 1,5x
5. Binance minimum emir tutarının altındaki pozisyon açılmaz.

**Risk kuralları**
- Portföy zirveden %10 düşerse tüm pozisyonlar kapatılır, bot kilitlenir ve Telegram'a acil uyarı gider.
- Günlük kayıp %3'e ulaşırsa pozisyonlar kapatılır; UTC gün sonuna kadar yeni pozisyon açılmaz.

## Test koşulları
- **Veri:** Binance USDⓈ-M 1 saatlik mumlar ve gerçek funding geçmişi (data.binance.vision), Ocak 2021 – Ekim 2026.
- **Maliyet:** her emirde %0,04 komisyon + %0,02 kayma, funding saat saat.
- **Bakiye:** 200 USDT.
- **Binance minimumları:** BTC 100$, ETH 20$, diğerleri 5$ (yaklaşık değerler; bot gerçek değerleri borsadan okur).
- **Koruma stopu:** 1 saatlik mumun tepe/dibiyle kontrol edildi, gap varsa kötü olan fiyattan çıkıldı.
- **Look-ahead yok:** karar, rebalance anından önce kapanmış mumlarla verilir.
- **Kod:** bot ile aynı fonksiyonlar (`backend/smcbot/momentum.py`). Script: `scripts/momentum_backtest.py`.

## Sonuçlar (200 USDT, Binance minimumları dahil)

| Ayar | Yıllık getiri | Sharpe | En büyük düşüş | Yıllık vol. | Ort. toplam nominal | 200$ → | Yıllar (2021 / 22 / 23 / 24 / 25 / 26) |
|---|---|---|---|---|---|---|---|
| Şartname (işaret şartı yok), risk kilidi kapalı | %4,7 | 0,43 | −%19,3 | %12,3 | %37 | 261$ | +8 / +17 / −4 / +15 / +6 / −12 |
| **İşaret şartı (varsayılan)**, risk kilidi kapalı | **%12,2** | **0,94** | **−%15,7** | %13,2 | %26 | **391$** | +15 / +16 / +13 / +17 / +13 / −1 |
| Şartname, %10 drawdown kilidi açık | %0,9 | 0,20 | −%10,0 | %5,6 | %4 | 211$ | 2022-02'de durdu |
| İşaret şartı, %10 drawdown kilidi açık | %2,1 | 0,40 | −%10,1 | %5,7 | %3 | 226$ | 2022-03'te durdu |

**Ek kontroller** (işaret şartı, kilit kapalı):

| Kontrol | Yıllık getiri | Sharpe | En büyük düşüş |
|---|---|---|---|
| Yalnız 2024–2026 | %9,7 | 0,76 | −%15,7 |
| Şartname, yalnız 2024–2026 | %2,5 | 0,28 | −%19,3 |
| Komisyon %0,05 (Binance standart taker) | %10,9 | 0,85 | −%16,1 |
| Rebalance 4 saatte bir | %8,9 | 0,69 | −%19,8 |
| Rebalance günde bir | %9,2 | 0,75 | −%22,9 |
| Minimum emir sınırı olmasaydı (büyük bakiye) | %11,9 | 0,87 | −%17,9 |

## Değerlendirme
1. **Şartnamedeki hâli zayıf.**
   - Yılda %4,7, 2026'da −%12.
   - İşaret şartı ile 2021–2025 her yıl artıda, 2026'da −%1,2.
   - Bu kural literatürde bilinen bir kuraldır ("dual momentum": hem göreli hem mutlak momentum).
   - Ancak 5 varyant içinden seçildi. Gelecekte bu kadar iyi olmayabilir; beklentiyi düşük tutun.
2. **%10 drawdown kilidi.**
   - Stratejinin normal en büyük düşüşü %16–20; %10 kilidi her iki ayarda da 2022'de devreye girdi.
   - Bot, kilitten sonra panelden "Kilidi kaldır" denmeden işlem açmaz. Bu bilinçli bir tercih: sermayeyi korur ama getiriyi de durdurur.
   - Kilidi daha seyrek görmek için `mom_max_dd_pct` artırılabilir (ör. %20). Bu, kabul edilen riski artırır.
3. **200$ ile minimum emir tutarı belirleyici.**
   - Coin başı tavan 30$ olduğu için BTC (minimum 100$) hiç açılamaz.
   - Hedeflerin bir kısmı 5$'ın altında kalır.
   - Ortalama toplam nominal bakiyenin yalnızca ~%26'sıdır (~50$).
4. **Gerçekçi beklenti:** iyi senaryoda yılda ~%10, yani 200$'da yılda ~20$. Günlük kâr hedefi koymaya uygun değildir.
5. **Rebalance sıklığı:** 8 saat, 4 saat ve günlükten iyi çıktı. Fark küçüktür ve gürültü olabilir.

## Sınırlamalar
- 8 coin ve tek bir piyasa dönemi (2021–2026) test edildi.
- Koruma stopu ve günlük limit saatlik mumlarla modellendi; gerçekte mark fiyatıyla tetiklenir.
- Binance minimumları test boyunca sabit kabul edildi.
- Geçmiş performans gelecek için garanti değildir.
