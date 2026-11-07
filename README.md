# Kripto Bot v4.3 — SMC Bot (4 saatlik trend takibi) + SMC Scalper (4s kırılım, %1 stop / %2 hedef)

## v4.3 — SMC Scalper: yeni strateji, süre kilidi kaldırıldı

**İstek:** hızlı işlemler, stop = kasanın %1'i, kâr = kasanın %2'si, 100 USDT, işlem başı %1 risk.

**Araştırma (12 ay 1 dakikalık veri, 15 coin; eğitim 2026-05..10, doğrulama 2025-11..2026-04):**
5 giriş kuralı (1dk momentum+hacim, 5dk kırılım+1s trend, trendde RSI dönüşü, 1dk likidite süpürme+geri alma,
rastgele giriş) × 4 stop genişliği (%0,5, %1, 1,5×ATR5, 3×ATR5), net 1:2 hedef. **20 kombinasyonun hepsi iki dönemde
de zararlı** (bugünkü emir tipleriyle işlem başı −0,15R … −0,43R; iyimser maker emirlerle bile −0,05R … −0,24R) ve
rastgele girişten farksız: bu vadede yön tahmin edilemiyor, komisyon+kayma riskin %15–40'ını yiyor. 100 USDT, 3 pozisyonla
en hızlı kurulum kasayı 3–5 günde, en iyisi ~3 haftada yarıya indiriyordu. Daha hızlı trend takibi de tutmadı:
1 saatlik kanal kırılımı doğrulamada −%0,2 … −%42, 15 dakikalık −%7 … −%91.

**Seçilen sistem:** 4 saatlik kanal kırılımı (ana botla aynı giriş) + istenen çıkış: stop kasanın %1'i, tek hedef
kasanın %2'si (komisyon sonrası net). 40 coin, %1 risk, en fazla 3 pozisyon: 2023 +%11, 2024 +%9, 2025 +%6,
2026 (Oca–Eki) +%8; maks. düşüş %10–13; haftada ~2 işlem, işlem ~4–5 gün sürer. Ana botun iz süren stopundan zayıf
ama her yıl artıda; ana bot demoda çalışırken bu sistem testnette **gerçek emirlerle** çalışır.

- `backend/smc/strategy/breakout.py`, `SMC_STRATEGY=breakout` (varsayılan; eski SMC için `smc`).
- Hedef fiyat dolum fiyatından hesaplanır (`risk/plan.py: net_target_price`): planlanan net RR tam 2,0.
- Risk her zaman %1 (`SMC_BO_RISK_PER_TRADE`); eski `SMC_RISK_PER_TRADE` bu stratejide kullanılmaz.
- Aynı 4s kapanışında en fazla 1 yeni işlem; Korku & Açgözlülük filtresi; ilişkili aynı yön pozisyon sınırı korunur.
- 72 saatlik deneme kilidi varsayılan olarak kapalı (`SMC_TRIAL_ENABLED=true` ile açılır).
- Strateji değişince SMC Scalper işlem verisi **bir kez** temizlenir (açık pozisyon yoksa): sanal kasa 100 USDT'den başlar.
- Not: 1 USDT riskle BTC/ETH gibi yüksek minimum tutarlı coinlerde pozisyon Binance alt sınırının altında kalabilir;
  bu işlemler "notional_below_min" ile atlanır.

## v4.2 — İyileştirme araştırması, görünürlük, günlük özet

**Araştırma yöntemi:** Önceden belirlenen 13 değişiklik, botun kurallarıyla (%1 risk, 3 pozisyon, aynı yönde 2, korku endeksi
filtresi, komisyon+kayma dahil) 40 coinde test edildi. Bir değişiklik ancak **hem 2025–26'da hem de hiç bakılmamış 2023–24'te**
getiri/maks. düşüş oranını iyileştirirse ve yakın eşik değerlerinde de tutarsa eklendi (30 farklı sinyal sırası, medyan).

| Değişiklik | 2025–26 getiri / maks. düşüş | 2023–24 getiri / maks. düşüş | Karar |
|---|---|---|---|
| Temel (v4.1) | +%57 / %16 | +%35 / %20 | — |
| **Aynı 4s mumda en fazla 1 yeni giriş** | +%58 / %14 | +%40 / %18 | **Eklendi** (varsayılan 1) |
| BTC yön filtresi (EMA200 / EMA50) | +%38 / %14 · +%31 / %13 | +%62 / %10 · +%45 / %17 | Eklenmedi (2025–26 bozuldu) |
| Mum tepeye yakın kapanmalı (üst %25) | +%84 / %13 | +%30 / %16 | Eklenmedi (%20/%33/%40 eşiklerinde doğrulama bozuldu: şans) |
| Hacim ≥ 1,0x / 1,5x | +%52 / %13 · +%48 / %13 | +%32 / %22 · +%22 / %17 | Eklenmedi |
| Aşırı uzamış kırılımı atla (>1 / >1,5 ATR) | +%58 / %12 · +%55 / %13 | +%23 / %21 · +%23 / %24 | Eklenmedi |
| 3R / 5R kârdan sonra stopu sıkılaştır | +%53 / %14 · +%59 / %15 | +%17 / %22 · +%16 / %22 | Eklenmedi |
| Zaman stopu (12 / 18 mum) | +%61 / %15 · +%49 / %16 | +%26 / %21 · +%46 / %21 | Eklenmedi |

Etkisi küçük ama tutarlı: aynı anda kırılan coinler çoğu zaman tek bir piyasa hareketidir; hepsine birden girmek aynı bahsi
büyütmektir. **Asıl fark risk ayarındadır:** aynı testte %5 risk + 5 pozisyon + yön sınırı yok, kasayı zirveden ~%92 düşürdü.

**Diğer değişiklikler:**
- Pozisyonlar sekmesi limit dolduğunda neden yeni işlem açılmadığını yazar; Tarayıcı son taramanın sonucunu gösterir
  (kaç kırılım, kaçı açıldı, kaçı hangi nedenle atlandı).
- Telegram: her gün 23:05'te (TR) günlük özet (equity, toplam K/Z, bugün kapananlar, açık pozisyonlar ve stopları).
  Ayarlar > Bağlantılar'da "Günlük özeti şimdi gönder". Telegram tutarları Türkçe biçimde.
- v4.1.2: Toplam K/Z gerçek başlangıç bakiyesinden; ayarlara geçerli aralık; "Test edilen risk ayarlarını doldur" düğmesi.

## v4.1 — Strateji değişti: 4 saatlik trend takibi

SMC (likidite avı + FVG) ve 15 dk destek/direnç + retest stratejileri gerçek veriyle test edildi ve **ikisi de
masraflardan sonra zararlı** çıktı (40 coin, 2025–2026; destek/direnç için 72 filtre kombinasyonunun hepsi eksi:
masrafsız ham avantaj ~+0,02R, komisyon+kayma işlem başı 0,06–0,36R). Bot artık varsayılan olarak
**4 saatlik trend takibi** kullanır; SMC panelden seçilebilir olarak duruyor (Ayarlar > Strateji).

**Kurallar** (`backend/smcbot/trend.py`):
- 4s mum, önceki **20** mumun tepesinin üstünde kapanırsa **long**, dibinin altında kapanırsa **short**; kapanışın hemen ardından piyasa emri.
- Başlangıç stopu **3 × ATR(14, 4s)**. Hedef yok: her 4s kapanışta stop son **10** mumun dibine/tepesine çekilir (sadece kâr yönünde).
- Korku & Açgözlülük: aşırı açgözlülükte (≥75) long, aşırı korkuda (≤25) short açılmaz. Hacim bilgi olarak gösterilir (filtre olarak tutarsızdı).
- Risk kuralları aynı (en fazla 3 pozisyon, aynı yönde 2, günlük limit, art arda stop soğuması, zirveden düşüş kilidi).
- **Kaldıraç otomatik düşer:** 3×ATR stoplar genişse (medyan ~%8) 10x'te likidasyon stoptan önce gelirdi. Kaldıraç
  `1 / (stop × 1,5 + %1)` ile sınırlanır (stop %4 → 10x, %8 → 7x, %14 → 4x). Risk değişmez; miktar stop mesafesinden hesaplanır.

**Test** (40 vadeli coin, %1 risk, botun canlı kurallarıyla; komisyon + kayma dahil). Ayarlar 2025–26 ile seçildi,
2023–24 hiç bakılmadan doğrulandı. Aynı 4s kapanışta çok coin birden kırıldığında hangi 3'ünün seçildiği sonucu
değiştirdiği için seçim 300 kez rastgele yapıldı:

| Yıl | Medyan getiri | En kötü %10 | Zararla biten senaryo | Tipik en derin düşüş |
|---|---|---|---|---|
| 2023 | +%5 | −%5 | %23 | %18 |
| 2024 | +%20 | +%12 | %0 | %8 |
| 2025 | +%30 | +%23 | %0 | %10 |
| 2026 (Oca–Eki) | +%22 | +%6 | %3 | %11 |

8.868 sinyal: ortalama +0,085R, kazanma oranı **%37**, medyan işlem −0,23R. Kazancın çoğu az sayıdaki büyük trendden
gelir; **uzun kayıp serileri normaldir**. Bu yüzden işlem başı risk **%1–2** olmalı (%5'te art arda 10 stop kasanın ~%40'ı).
Uyarılar: coin listesi bugünün popüler coinleri (2023–24 sonuçlarını iyimser gösterebilir); geçmiş sonuç geleceği garanti etmez.

---

# v4 — SMC Bot kurulumu (hâlâ geçerli)

Fonlama (funding) botu ve paneli **kaldırıldı**. Yerine **SMC Bot** geldi; paneli artık ana sayfa (`/`).
**SMC Scalper** (`backend/smc`, panel `/smc/`) ve kapalı scalp/day/swing motorları olduğu gibi duruyor.

| Servis | Ne çalışır | Panel |
|---|---|---|
| `crypto-bot-dashboard` (web) | **SMC Bot** motoru + tüm paneller | `/` SMC Bot · `/smc/` SMC Scalper |
| `crypto-bot-engine` (worker) | SMC Scalper (+ kapalı yönlü motorlar) | — |

### SMC stratejisi (eski, panelden seçilebilir)
En hacimli 30 vadeli coini her 15 dk mum kapanışında tarar: likidite avı → MSS → FVG (SMC/ICT) yapısını
order flow verisiyle (CVD, Binance likidasyon akışı, tahmini likidasyon haritası, OI, funding, emir defteri) puanlar.
10x kaldıraçla long/short açar, 1R'de yarısını kapatıp stopu girişe çeker, hedefi bir sonraki likidite seviyesidir.
Her işlemin grafiği (av, MSS, FVG, giriş/stop/TP, çıkış) ve "neden girildi" listesi panelde görünür.
Telegram'a `[SMC Bot · DEMO]` etiketiyle bildirim gider (SMC Scalper'ınkiler `[SMC·TESTNET]`).

- **Modlar:** Demo (varsayılan; gerçek fiyat, sanal 1000 USDT, emir yok) → Testnet → Gerçek.
- **Gerçek mod kilitli:** demoda en az 100 işlem, pozitif K/Z ve profit factor ≥ 1,2 olmadan açılmaz.
- **Risk kuralları:** stop mesafesine göre pozisyon büyüklüğü, günlük zarar limiti, art arda 3 stopta 4 saat soğuma,
  zirveden %30 düşüşte otomatik durma, en fazla 3 pozisyon. İşlem başı risk varsayılanı **%5** (panelden değişir; %1–2 önerilir).
- **İlk açılışta bot DURAKLATILMIŞTIR:** panelde **Başlat**'a basınca demo modda işlem aramaya başlar.

### SMC stratejisinin test sonucu
Mayıs–Ekim 2026, en hacimli 15 coin, 15 dk, komisyon + kayma dahil, işlem başı %1 risk:
170 işlem, ortalama **−0,10R**, profit factor **0,79**. Mayıs–Temmuz −0,29R, Ağustos–Ekim +0,10R.
Yani tutarlı bir avantaj henüz görünmüyor; %5 riskle aynı dönem kasayı −%65'e götürdü.
Canlı order flow verisi geçmişte olmadığı için testte yok; etkisini ancak demo gösterir. Panelde
**Geçmiş test** sekmesinden güncel veriyle tekrar test edilebilir.

### Kurulum (mevcut Render kurulumuna ek)
1. Bu sürüm main'e gelince Render iki servisi de yeniden kurar. Ek veritabanı veya disk gerekmez:
   SMC Bot mevcut Postgres'i `smcbot_` önekli tablolarla kullanır.
2. **Telegram:** `render.yaml`, dashboard servisine `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID`'yi worker'dan
   otomatik kopyalar. Panel > Ayarlar > "Telegram'a deneme mesajı gönder" ile kontrol et. Mesaj gelmezse Render >
   crypto-bot-dashboard > Environment'a bu iki değeri elle ekle.
3. **Testnet modu** dashboard'daki mevcut `BINANCE_API_KEY`/`SECRET`'i (testnet anahtarı) kullanır.
   Ayrı anahtar istersen `SMCBOT_TESTNET_API_KEY`/`SECRET` ekle. Testnet hesabı SMC Scalper ile ortaktır:
   SMC Bot, başka pozisyon olan coinde işlem açmaz ve sadece kendi stop/TP emirlerini iptal eder.
4. **Gerçek mod** için ayrı anahtar gerekir: `SMCBOT_LIVE_API_KEY` / `SMCBOT_LIVE_API_SECRET`
   (sadece Futures izni, **çekim izni kapalı**, IP kısıtlı). Render bu `sync: false` değişkenleri mevcut servise
   otomatik eklemez; gerektiğinde Environment'tan elle ekle.
5. İsteğe bağlı: `COINGLASS_API_KEY` (ücretli) eklenirse CoinGlass likidasyon ısı haritası da hedef seçiminde kullanılır.

| Değişken (dashboard servisi) | Varsayılan | Açıklama |
|---|---|---|
| `SMCBOT_ENABLED` | `true` | `false` yapılırsa SMC Bot hiç çalışmaz |
| `SMCBOT_TESTNET_REST_URL` | `https://demo-fapi.binance.com` | Testnet REST adresi |
| `SMCBOT_ALLOW_LIVE_WITHOUT_GATE` | `false` | Gerçek mod kilidini kaldırır (önerilmez) |
| `SMCBOT_OFFLINE_SIM` | `false` | Geliştirme: Binance yerine sentetik piyasa |

### Render deploy geçişi
Render yeni sürümü açarken eski kopya kısa bir süre daha çalışır. Aynı anda iki kopyanın işlem açmaması için
veritabanında bir **yönetim kilidi** (`smcbot_lease`) var: işlemi sadece kilidi tutan kopya yapar. Eski kopya
kapanırken kilidi bırakır, yenisi birkaç saniyede devralır. Bu arada panelde "Bu sunucu kopyası yedekte" yazabilir.

### Kaldırılanlar
- `backend/funding/`, funding paneli ve `/api/summary`, `/api/funding`, `/api/cumulative` vb. eski panel API'leri.
- Eski panelde yönlü (scalp/day/swing) motor işlemleri de görünüyordu; bu motorlar varsayılan kapalı olduğu için
  yeni panelde yok. Veritabanındaki `funding_positions` ve `trades` tabloları silinmedi, veriler duruyor.

### Testler
```bash
pip install -r requirements.txt pytest && python -m pytest tests     # internet gerekmez
python -m pytest tests/smcbot                                        # sadece SMC Bot
```

---

# v3.2 — SMC Scalper ve şifreli panel (hâlâ geçerli)

v3.2'de eklenen SMC Scalper v4'te de aynen çalışıyor (fonlama motoru v4'te kaldırıldı).
Bunlara **dokunulmadı**; şartnamedeki scalping botu `backend/smc/` altına ayrı bir modül olarak eklendi.

**Nasıl çalışır?**
- SMC, mevcut worker'ın (`python -m backend.main`) içinde **ayrı bir thread**'de, kendi asyncio döngüsüyle çalışır.
  Ek Render servisi gerekmez. SMC'de hata olursa thread kendini yeniden başlatır, funding motoru etkilenmez.
- Binance testnet key'i, Telegram ve veritabanı mevcut botla **ortak**. SMC tabloları `smc_` önekli, mevcut tablolarla çakışmaz.
- Telegram mesajları `[SMC·TESTNET]` etiketiyle gelir.
- Yalnızca Binance Futures **Testnet/Demo**'ya emir gönderir; mainnet adresi kod tarafından reddedilir.

**Strateji ve risk (şartname):** 15m/5m/3m SMC/ICT (BOS, CHoCH, liquidity sweep, OTE %62–79, FVG, displacement,
synthetic delta/CVD, OI, funding, Fear & Greed), 15 puanlık skor (min 9). İşlem başına %5 risk (dinamik, komisyon +
slippage dahil), toplam açık risk %15, günlük zarar %10, max 3 pozisyon, 3 ardışık SL → 30 dk cooldown, ≤10x isolated,
liquidation buffer, native STOP_MARKET SL + TP1/TP2/TP3 (%50/%25/%25, 1R/2R/3R), TP1 sonrası breakeven,
partial fill yönetimi, WebSocket + reconnect, rate limiter, restart reconciliation, kill switch, 72 saatlik trial.
SMC kendi **sanal 100 USDT** kasasıyla çalışır (mevcut botun kasasından ayrı).

**Aynı testnet hesabı (`SMC_ACCOUNT_SCOPE=shared`, varsayılan):** scalp/day/swing motorlarını açarsan ikisi aynı
hesapta işlem yapar. SMC başka bir botun pozisyonu olan sembolde işlem açmaz; kill switch ve reconciliation yalnızca
SMC'nin kendi pozisyonlarına ve `sb` önekli emirlerine dokunur.

**72 saatlik trial:** Binance bağlantısı ve API key doğrulandıktan sonra başlar (key yanlışsa süre akmaz).
Restart süreyi sıfırlamaz. Süre bitince SMC pozisyonlarını kapatır ve durur; funding motoru çalışmaya devam eder.
Yeniden başlatmak için: `python reset_bot.py --smc`.

### Kurulum (mevcut Render kurulumuna ek)
1. Panel şifresi için hash üret (bilgisayarında): `pip install -r requirements.txt && python -m scripts.smc.hash_password`
2. GitHub'a push et. Render Blueprint `render.yaml`'ı günceller; dashboard servisinde şunları gir:
   `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD_HASH` (1. adımdaki satır). `SESSION_SECRET` otomatik üretilir.
3. Worker'da `BINANCE_API_KEY` / `BINANCE_API_SECRET` zaten varsa başka bir şey gerekmez (`SMC_ENABLED=true`).

### Panel
- **Tüm panel artık şifreli** (eskiden adresi bilen herkes açabiliyor ve işlem kapatabiliyordu). `/health` şifresizdir.
- SMC Bot paneli: `/` · SMC Scalper paneli: `/smc/` (SMC Bot panelinde "SMC Scalper" bağlantısı). SMC panelinde hesap/risk özeti,
  açık pozisyonlar, interaktif grafik (entry/SL/TP, sweep, BOS/CHoCH, OTE, FVG), trade replay ve **KILL SWITCH** var.

### SMC strateji modu (`SMC_PROFILE`)
- **active (varsayılan):** Zorunlu kontrollerin hepsi (15dk trend, likidite süpürmesi, 5dk kırılım, OTE, 3dk onay)
  ve risk kuralları (%5 / %15 / %10, 10x) aynı kalır; yalnızca eşikler gevşer: OTE %50–79, OTE dokunuşu son 12 adet
  3m mumda, onay mumu son 1 mumu kırmalı, stop en fazla %4, süpürme son 5 saatte aranır, kayma tahmini %0.02,
  maliyet sonrası min kazanç/risk 1.0.
- **spec:** Şartname eşikleri birebir (OTE %62–79, 6 mum, 2 mum kırılım, stop ≤ %3, 3 saat, kayma %0.05, RR ≥ 1.2). Çok seçici, işlem nadir.

### SMC ayarları
Tüm SMC parametreleri `SMC_` önekiyle değiştirilir (örn. `SMC_MIN_SCORE=10`, `SMC_RISK_PER_TRADE=0.02`).
Tam liste: `backend/smc/config.py`. SMC'yi kapatmak için `SMC_ENABLED=false`.

### Backtest ve testler
```bash
python -m scripts.smc.download_history --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 90 --out data/smc
python -m scripts.smc.run_backtest --data data/smc --symbols BTCUSDT,ETHUSDT,SOLUSDT --out reports/smc
pip install pytest && python -m pytest tests                     # mevcut + SMC testleri (internet gerekmez)
RUN_TESTNET_INTEGRATION=1 python -m pytest tests/smc/integration -s   # gerçek testnet emirleriyle
```
Backtest live ile aynı strateji/risk/TP/BE kodunu kullanır; veri training / validation / out-of-sample olarak ayrılır.
**Testnet'te çalıştırmadan önce gerçek veriyle backtest yap:** v2'de kısa vadeli grafik kurulumları komisyon sonrası
kazanç göstermemişti (aşağıdaki "Neden strateji değişti?"); SMC kurulumları da benzer bir alanda.

Şartname maddelerinin koddaki karşılıkları: `docs/SMC_ACCEPTANCE.md`.

---

# Kripto Bot v3 — Fonlama Oranı (Funding) Stratejisi

**v4'te kaldırıldı.** Yerine SMC Bot geldi (yukarıya bak).

---

# Kripto Trading Bot v2 — AI Filtreli, "Küçük ama Net Kazanç" Modu

## v2'de ne değişti?

**Düzeltilen hatalar**
- Sinyaller artık sadece **kapanmış mumlarla** hesaplanıyor. Eskiden oluşmakta olan mum kullanılıyordu ve sinyaller kayboluyordu.
- Pivotlar önceki **tam günden** hesaplanıyor. Eski kod tek mumdan hesapladığı için sürekli sahte "long" sinyali üretiyordu.
- VWAP her UTC gününde **sıfırlanıyor**.
- **Komisyon ve kayma** her hesapta var: backtest, AI etiketi ve PnL. Kapanan işlemin gerçek net PnL'i borsadan okunuyor.
- SL/TP **gerçek dolum fiyatına** göre konuyor.
- Stop taşınırken önce yeni stop konuyor, sonra eskisi iptal ediliyor. Böylece pozisyon arada korumasız kalmıyor.
- Dashboard ve bot aynı işlemi iki kez kasaya işleyemiyor.

**Strateji / çıkış**
- "0.5R'de başa-baş, %40 geri çekilmede kapat" sistemi kaldırıldı. Kazançları kırpıp zararları tam bırakıyordu.
- Tek hedef: **1.2R (scalp) / 1.5R (day)**. Ayrıca bir zaman stopu var: scalp 3 saat, day 8 saat.
- 1 dakikalık scalp kaldırıldı. Scalp artık 5m/1h, day 15m/4h zaman dilimlerinde çalışıyor.
- Birbiriyle çelişen oylama sistemi yerine 3 net kurulum var: `trend_pullback`, `range_reversion`, `liquidity_sweep`.

**Risk**
- İşlem başına risk kasanın **%1**'i. Eski ayarda sabit 5$/10$ vardı, bu 100$ kasada %5-10 risk demekti.
- Kill-switch tekrar açık: günlük %5 zarar veya art arda 4 kayıpta motor 4 saat duruyor.
- Toplam açık risk en fazla %4, aynı yöndeki (tüm altcoinler dahil) risk en fazla %3.
- Kaldıraç otomatik: marja sığan en düşük kaldıraç seçiliyor, üst sınır 10x. Likidasyon fiyatı stoptan en az 3 kat uzakta tutuluyor.

**Yapay zeka**
- Her aday sinyal için model şu soruya cevap veriyor: "Bu işlem komisyon sonrası kâr eder mi?" (Gradient Boosting)
- Model gerçek geçmiş veride eğitiliyor ve **hiç görmediği bir test döneminde** sınanıyor.
- **Testi geçemeyen model onay almaz. Onaysız modelle bot o motorda işlem AÇMAZ** (`AI_MODE=required`).
- Açılan her işlemin özellikleri veritabanına kaydediliyor (`features_json`). İleride bu kayıtlarla gerçek işlemlerden yeniden eğitim yapılabilir.

---

## ADIM A: Modeli eğit (kendi bilgisayarında)

İnternet bağlantısı gerekir, API key gerekmez. Gerçek Binance verisi indirilir.

```bash
pip install -r requirements.txt
python -m backend.ai.train --engine scalp --days 365
python -m backend.ai.train --engine day --days 730
```

İlk çalıştırma veri indirdiği için 10-30 dakika sürebilir. Sonraki çalıştırmalar `data/` klasöründeki önbelleği kullanır.

## ADIM B: Raporu oku

Ekranda ve `models/<motor>_report.json` dosyasında şunlar yazar:

- **AUC**: 0.50 yazı-tura demektir. 0.55'in üstü anlamlı bir sinyal var demektir. 0.70'in üstü şüphelidir, bana bildir.
- **AI'sız / AI filtreli**: test dönemindeki işlem sayısı, kazanma oranı, ortalama R (komisyon sonrası), profit factor, maksimum düşüş.
- **✅ ONAYLANDI** veya **❌ onaylanmadı** sonucu.

"Onaylanmadı" çıkarsa bot o motorla işlem açmaz. Bu bir hata değil, koruma: stratejinin o piyasada avantajı yok demektir, para kaybetmeden öğrenmiş olursun.

## ADIM C: Modeli GitHub'a yükle ve deploy et

Render modeli repodan okur. Onaylanan modelleri commit'le:

```bash
git add models/ && git commit -m "AI modelleri" && git push
```

## ADIM D: En az 2-4 hafta testnette izle

Testnet sonuçlarını raporla karşılaştır. Kazanma oranı ve ortalama R, test dönemine yakın olmalı. Çok daha kötüyse canlıya geçme.

## Bakım

- Modeli **ayda bir** yeniden eğit, çünkü piyasa rejimi değişiyor.
- `config.py` içinde `EXIT_PARAMS` veya `ENGINE_SETUPS` değiştirirsen **mutlaka yeniden eğit**. Model eski kurallara göre eğitilmişse yanlış karar verir.
- `AI_MIN_PROBABILITY` ile daha seçici olabilirsin: daha az ama daha kaliteli işlem.

## ⚠️ Dürüst uyarılar

- Backtest geçmişi ölçer, geleceği garanti etmez.
- Scalp'ta komisyon ve kayma işlem başına yaklaşık **0.2R** tutar. Avantajı olmayan bir sinyal bu yüzden kesin kaybettirir. AI filtresinin görevi tam olarak bunu elemek.
- Eğitimde bugünün en hacimli coinleri kullanılıyor. Bu durum sonuçları biraz iyimser gösterebilir (hayatta kalma yanılgısı).
- Gerçek paraya geçmeden önce kaybetmeyi göze alabileceğin küçük bir tutarla başla.

---

# İlk Kurulum Rehberi (Faz 1 - hâlâ geçerli)

Bu rehber, hiç deneyimin olmadığını varsayarak yazıldı. Her adımı sırayla, atlamadan yap.

---

## 📌 ŞU ANDA NEREDEYİZ?

Faz 1'de sadece **altyapıyı** kurduk:
- Proje klasör yapısı
- Veritabanı şeması
- Binance Testnet bağlantısı
- İlk 2 teknik gösterge (EMA, VWAP)

**Henüz gerçek/testnet işlem açılmıyor.** Bu fazın amacı: "bağlantılar çalışıyor mu?" diye test etmek.

---

## ADIM 1: GitHub Reposu Oluştur

1. https://github.com adresine git, hesabın yoksa ücretsiz aç.
2. Sağ üstte **+** işaretine tıkla → **New repository**.
3. İsim ver (örn: `crypto-bot`), **Private** seç (herkese açık olmasın, API key'lerin olmasa bile güvenlik için).
4. **Create repository** butonuna bas.
5. Oluşan boş repo sayfasında "…or push an existing repository" kısmındaki komutları not al (birazdan kullanacağız).

Bana repo adını/linkini verirsen, dosyaları oraya nasıl yükleyeceğini de adım adım gösteririm.

---

## ADIM 2: Binance Testnet Hesabı ve API Key

**Dikkat:** Bu gerçek Binance hesabından FARKLI bir sistemdir, sahte parayla çalışır.

1. https://testnet.binancefuture.com adresine git.
2. Sağ üstten GitHub hesabınla giriş yap (Binance testnet, GitHub ile giriş istiyor - normal).
3. Giriş yaptıktan sonra sana otomatik olarak sahte USDT bakiyesi verilir (genelde 10.000-15.000 arası).
4. Üst menüden **API Key** bölümüne git (bazen "API Management" olarak geçer).
5. Yeni bir API key oluştur, **Key** ve **Secret** değerlerini kopyala.
   - ⚠️ Secret sadece bir kere gösterilir, kaybedersen yeni key oluşturman gerekir.

---

## ADIM 3: Telegram Bot Oluştur

1. Telegram uygulamasında **@BotFather** adlı hesabı bul (arama kutusuna yaz).
2. `/newbot` komutunu gönder.
3. Bota bir isim ver (örn: "Kripto Bot Bildirim").
4. Bir kullanıcı adı ver (sonu "bot" ile bitmeli, örn: `kriptobot_bildirim_bot`).
5. BotFather sana bir **token** verecek (şuna benzer: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`). Kopyala.
6. Şimdi kendi **Chat ID**'ni bulman lazım:
   - Oluşturduğun bota Telegram'dan git, herhangi bir mesaj gönder (örn: "merhaba").
   - Tarayıcıda şu adrese git (TOKEN yerine kendi token'ını yaz):
     `https://api.telegram.org/botTOKEN/getUpdates`
   - Açılan sayfada `"chat":{"id":123456789,...}` gibi bir kısım göreceksin. O sayı senin Chat ID'n.

---

## ADIM 4: Dosyaları Bilgisayarına İndir ve .env Doldur

1. Aşağıda paylaştığım ZIP dosyasını indir, bir klasöre çıkart (unzip).
2. İçindeki `.env.example` dosyasının bir kopyasını oluştur, adını `.env` yap (nokta ile başlıyor, dikkat).
3. `.env` dosyasını bir metin editörüyle aç (Not Defteri yeterli) ve şunları doldur:
   - `BINANCE_API_KEY` → Adım 2'deki key
   - `BINANCE_API_SECRET` → Adım 2'deki secret
   - `TELEGRAM_BOT_TOKEN` → Adım 3'teki token
   - `TELEGRAM_CHAT_ID` → Adım 3'teki chat id
4. Kaydet.

---

## ADIM 5: GitHub'a Yükle

Bilgisayarında terminal/komut satırı aç (Windows'ta "cmd" veya "PowerShell", Mac'te "Terminal"), indirdiğin klasöre girip şu komutları sırayla çalıştır:

```bash
git init
git add .
git commit -m "Faz 1: temel altyapı"
git branch -M main
git remote add origin <GITHUB_REPO_LINKIN>
git push -u origin main
```

`<GITHUB_REPO_LINKIN>` yerine Adım 1'de oluşturduğun reponun linkini yaz (örn: `https://github.com/kullaniciadi/crypto-bot.git`).

**Not:** `.env` dosyası `.gitignore` sayesinde otomatik olarak yüklenmeyecek — bu bilinçli bir güvenlik önlemi, API key'lerin GitHub'da görünmemeli.

---

## ADIM 6: Lokal Test (Opsiyonel ama Önerilir)

Eğer bilgisayarında Python varsa (yoksa bu adımı atlayıp direkt Render'a geçebiliriz):

```bash
pip install -r requirements.txt
python -m backend.main
```

Ekranda şöyle bir çıktı görmen lazım:
```
✅ Veritabanı tabloları hazır.
✅ Binance bağlantısı OK. Testnet USDT bakiyesi: 15000.0
📊 BTC/USDT | EMA: long_weak | VWAP: neutral
📊 ETH/USDT | EMA: short_weak | VWAP: long
```

Bu çıktıyı görürsen Faz 1 başarılı demektir.

---

## ⏭️ SIRADAKİ FAZLAR (henüz yapılmadı)

- **Faz 2:** 3 motor (scalp/day/swing), confluence sinyal mantığı, kalan 4 teknik (SMC, likidite bölgeleri, korku-açgözlülük, destek/direnç)
- **Faz 3:** Risk yönetimi otomasyonu (breakeven, trailing/momentum kaybı kapama, kill-switch, korelasyon filtresi, haber filtresi)
- **Faz 4:** Telegram bildirim akışı (açılış + reply-thread güncellemeleri + heartbeat + haftalık özet)
- **Faz 5:** Dashboard (React + mum grafiği)
- **Faz 6:** Render'a tam deploy + canlı testnet çalıştırma

Faz 1'i test edip bana sonucu (çalıştı mı, hata aldın mı) bildirdiğinde Faz 2'ye geçeceğim.
