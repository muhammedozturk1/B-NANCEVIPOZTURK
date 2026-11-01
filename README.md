# Kripto Bot v3.2 — Funding + SMC Scalper

## v3.2 — Yeni: SMC Scalper (şartname v2.1) ve şifreli panel

Mevcut bot aynen çalışmaya devam ediyor (fonlama motoru, scalp/day/swing, AI filtresi).
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
- Funding paneli: `/` · SMC paneli: `/smc/` (soldaki menüde "SMC Paneli"). SMC panelinde hesap/risk özeti,
  açık pozisyonlar, interaktif grafik (entry/SL/TP, sweep, BOS/CHoCH, OTE, FVG), trade replay ve **KILL SWITCH** var.

### SMC strateji modu (`SMC_PROFILE`)
- **active (varsayılan):** Zorunlu kontrollerin hepsi (15dk trend, likidite süpürmesi, 5dk kırılım, OTE, 3dk onay)
  ve risk kuralları (%5 / %15 / %10, 10x) aynı kalır; yalnızca eşikler gevşer: OTE %50–79, OTE dokunuşu son 12 adet
  3m mumda, onay mumu son 1 mumu kırmalı, stop en fazla %4, süpürme son 5 saatte aranır.
- **spec:** Şartname eşikleri birebir (OTE %62–79, 6 mum, 2 mum kırılım, stop ≤ %3, 3 saat). Çok seçici, işlem nadir.

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

## v3.1 — Düzeltmeler
- **Bot artık testnet'e takılıp durmuyor.** Eskiden açılışta testnet bakiyesi okunamazsa TÜM bot kapanıyordu;
  oysa fonlama motoru (paper) API key'e hiç ihtiyaç duymuyor. Testnet bağlantısı artık sadece yönlü motorlar açıksa gerekli.
- **"Bot Çalışıyor" rozeti gerçek.** Worker 5 dakikada bir veritabanına sinyal yazıyor; 15 dakika sessiz kalırsa rozet kırmızıya döner.
- **Fonlama taraması görünür.** Dashboard'da son tarama zamanı, kaç coinin incelendiği, en iyi adayların oranı ve
  neden girilmediği yazıyor. Son hata da gösteriliyor.
- **Dashboard:** kümülatif grafik artık gerçek tarihli ve fonlama K/Z'sini de içeriyor; tarihler 3 saat kayık
  görünmüyor (UTC işareti eklendi); ROI komisyon dahil net K/Z'den hesaplanıyor (eskiden PnL eksiyken ROI artı çıkabiliyordu);
  yeni "R" sütunu (net K/Z / risk); "Erken Kapama" etiketi "Zaman stopu / Elle" oldu.
- Telegram heartbeat ve haftalık özet fonlama pozisyonlarını da içeriyor.
- `backend/lab.py` olmayan modülleri import ettiği için hiç çalışmıyordu, düzeltildi.
- `tests/` klasörü: internet gerektirmeyen testler -> `pip install pytest httpx && python -m pytest tests`


## Neden strateji değişti?
v2'deki kısa vadeli grafik kuralları (EMA geri çekilme, likidite süpürme, Bollinger dönüşü),
100 coin ve 2-3 yıllık veride, üç farklı zaman diliminde test edildi. Komisyon sonrası
hiçbiri tutarlı kazanç göstermedi. Bu motorlar artık varsayılan olarak KAPALI.

## Yeni strateji nasıl kazanır?
Vadeli işlemlerde long ve short tutanlar her 1/4/8 saatte bir birbirine "fonlama" öder.
Oran pozitifken long'lar short'lara öder. Bot aynı coini **spot'ta alır** ve vadelide
**aynı miktar short açar**. Fiyat ne yöne giderse gitsin iki bacak birbirini dengeler;
bot sadece fonlama ödemelerini toplar. Fiyat tahmini yoktur.

- Giriş: son 3 günün ortalama fonlaması yıllık %15+ ve hiç negatif ödeme yok
- Çıkış: ortalama yıllık %3 altına düşerse, fonlama negatife dönerse veya fiyat %30 oynarsa
- En az 3 gün tutulur. DİKKAT: giriş-çıkış maliyeti ~%0.42; yıllık %15 fonlamayla bu ancak ~10 günde çıkar
  (eski metindeki "3 gün yeter" hesabı yanlıştı). Bot artık her aday için bu süreyi gösteriyor.
- En fazla 5 pozisyon, her biri kasanın %12'si (vadeli bacak 2x teminatla)

## Mod: paper (sanal)
Gerçek Binance fiyat ve fonlama verisiyle sanal işlem yapılır, emir gönderilmez.
Testnetin fonlama oranları gerçek olmadığı için bu strateji için paper modu testnetten
daha gerçekçidir. Gerçek parayla çalıştırma (live) ayrı bir adım olarak eklenecek.

## Backtest (Codespace'te)
```bash
python -u -m backend.funding.backtest --days 1095
```

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
