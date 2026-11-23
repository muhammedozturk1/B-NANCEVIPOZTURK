# Trend çıkış karşılaştırması (eski vs yeni)

Veri (ücretsiz, data.binance.vision): 20 coin, 2020-01..2026-10, 1 saatlik kline + fundingRate.
`TREND_DATA` klasöründe `{SYM}_1h.csv` ve `{SYM}_funding.csv` olmalı (sütunlar Binance zip'lerindeki gibi).

```
export TREND_DATA=/yol/long
python scripts/trend_compare/run.py <ESKİ_REPO> orig orig.pkl          # orijinal kod (4s, funding yok)
python scripts/trend_compare/run.py . legacy legacy.pkl                # eski kurallar, aynı maliyet + funding
python scripts/trend_compare/run.py . NEW_default NEW_default.pkl      # gönderilen varsayılanlar
python scripts/trend_compare/metrics.py . orig legacy NEW_default
```
Varyantlar `variants.py` içinde, sonuçlara bakılmadan yazıldı. Seçim yalnız IS 2020-2023, OOS 2024-2026 doğrulama.
