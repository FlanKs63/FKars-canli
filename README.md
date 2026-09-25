# Canlı Alarm — Furkanla Katla

Sürekli çalışan canlı alarm botu. Her 30 saniyede 1 dakikalık mumlara bakar; sadece şu
durumlarda Telegram'a yazar:

- **Hacim patlaması:** son 5 dakikanın hacmi önceki saatin ortalamasının 5 katı
- **Ani sıçrama:** son 5 dakikada %3+ (kripto %2+)
- **Gün içi zirve kırılımı:** hacimle birlikte günün zirvesi aşıldı

Aynı hisseye 45 dakika tekrar yazmaz (fiyat son alarmdan %5 daha yükselirse yazar), saatte en
fazla 15 mesaj. İzlenenler: ABD hisseleri + günün en çok yükselen / en aktif / küçük şirket
listeleri (0,5-20 $, ön piyasa 11:00 TR'den itibaren), kripto (7/24), BIST (~15 dk gecikmeli).
Alarm çıkınca Gemini o hisse için internette haber arar (anahtar varsa).

## Sinyal filtreleri (`sinyal_filtreleri.py`)
Alarm bulununca mesaj hemen gitmez; o hissenin 5 dakikalık mumlarıyla filtreden geçer. Geçemezse
gönderilmez, nedeni log'a `ATLANDI XYZ: ...` diye yazılır. Kurallar:
- Kırılım mumu hacimli olmalı (son 20 mumun 1,5 katı), kapanış kırılan seviyenin üstünde olmalı
- Fiyat VWAP üstünde, ama VWAP'tan 2 ATR'den fazla uzaklaşmamış olmalı
- Tepede hacimli doji olmamalı; risk %6'dan büyük olmamalı; TP1'den önce direnç olmamalı
- Günlük RVOL: ABD ≥ 5x, BIST ≥ 3x (kripto'da uygulanmaz)

**Zorunlu / esnek kurallar:** Kırılımın fitilde kalması, fiyatın VWAP altında olması, tepede hacimli doji,
risk > %6 ya da 0,10 $ altı fiyat **zorunlu** kurallardır; biri bile bozulursa mesaj gitmez. Günlük RVOL,
mum hacmi, VWAP'tan uzaklık ve TP1 önünde direnç **esnek** kurallardır: yalnız biri bozuksa sinyal
`Sinyal: ORTA ⚠️ (neden)` etiketiyle gider, hepsi tamamsa `GÜÇLÜ ✅`. `LIVE_MAX_SOFT=0` → her kural zorunlu.

Geçen sinyalde giriş / kademe / stop filtrenin seviyeleriyle yazılır (TP'ler riskin 1,5 / 2,5 / 4 katı).
Haber: ABD hisselerinde `FINNHUB_KEY` varsa son 24 saatin başlığı (`📰 Haber yok (dikkat)` da olabilir),
yoksa Gemini araştırır; Gemini kotası dolarsa Google Haberler'in son 24 saatlik başlıkları eklenir.

**Çıkış takibi:** Gönderilen sinyal 6 saat izlenir (5 dk'da bir). Stop çalışırsa, sahte kırılım,
hacimsiz yükseliş, hacimli doji, VWAP altı kapanış ya da büyük kırmızı mum olursa
`🚪 $XYZ ÇIKIŞ: ...` mesajı gider. TP1 görülünce stop girişe çekilir, sonra iz süren stop.

Ayarlar: `LIVE_FILTERS=0` (filtreleri kapatır, eski davranış) · `LIVE_MAX_SOFT` (1) · `LIVE_RVOL_US` (5) · `LIVE_RVOL_BIST` (3) ·
`LIVE_TRACK_HOURS` (6) · `LIVE_EXIT_CHECK_SEC` (300) · `LIVE_KASA` (ör. 1000 → adet önerisi).
Eşikleri değiştirmek için `sinyal_filtreleri.py` başındaki sabitler düzenlenir.

## KAP bildirimleri, takvim ve Telegram komutları
- **📢 KAP:** Takip listesindeki BIST şirketlerinin önemli bildirimleri (özel durum, finansal rapor, kâr payı,
  sermaye artırımı, geri alım, yeni iş ilişkisi, ihale…) 2 dakikada bir kontrol edilir ve link ile gönderilir.
  Rutin bildirimler (genel kurul ilanı vb.) gönderilmez. `KAP_ALERTS=0` kapatır, `KAP_MAX_PER_HOUR` (10).
- **📅 Takvim:** Her iş günü 09:00'da önümüzdeki 3 günün bilanço ve temettü (hak düşüm) tarihleri
  (ABD + BIST takip listesi). Olay yoksa mesaj gitmez. `TAKVIM=0` kapatır, `TAKVIM_HOUR`, `TAKVIM_DAYS`.
- **🤖 Komutlar** (gruba yaz, ~30 sn içinde cevap gelir): `/durum` · `/fiyat THYAO` (ya da AAPL, BTC) ·
  `/takvim` · `/yardim`. Sadece bu gruptan gelen komutlar işlenir. `TG_COMMANDS=0` kapatır.

## Kurulum
1. Settings → Secrets and variables → Actions: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   isteğe bağlı `GEMINI_API_KEY` ve `FINNHUB_KEY` (finnhub.io'dan ücretsiz).
2. Settings → Actions → General → Workflow permissions: **Read and write permissions**.
3. Actions sekmesinde workflow'ları etkinleştir. "Canlı alarm" bir kez başlayınca ~5 saat 40 dk
   çalışır ve bitince bir sonrakini kendisi başlatır (kesintisiz). Zincir koparsa yedek zamanlayıcı
   (her saat :13 ve :43) yeniden başlatır. Hemen başlatmak için **Run workflow**.

## Ayarlar (workflow `env:` kısmına)
`LIVE_MARKETS` (us,kripto,bist) · `LIVE_POLL_SECONDS` (30) · `LIVE_COOLDOWN_MIN` (45) ·
`LIVE_REARM_PCT` (0.05) · `LIVE_MAX_PER_HOUR` (15) · `LIVE_JUMP_PCT` · `LIVE_VOLUME_MULT` (5) ·
`LIVE_MIN_PRICE` (0.5) · `LIVE_MAX_PRICE` (20) · `LIVE_NEWS_PER_HOUR` (6) · `SIGNATURE`

Testler: `python -m unittest discover -s tests -v`
