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

## Kurulum
1. Settings → Secrets and variables → Actions: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   isteğe bağlı `GEMINI_API_KEY`.
2. Settings → Actions → General → Workflow permissions: **Read and write permissions**.
3. Actions sekmesinde workflow'ları etkinleştir. "Canlı alarm" her saat başı kendiliğinden başlar
   ve 55 dakika çalışır; elle başlatmak için **Run workflow**.

## Ayarlar (workflow `env:` kısmına)
`LIVE_MARKETS` (us,kripto,bist) · `LIVE_POLL_SECONDS` (30) · `LIVE_COOLDOWN_MIN` (45) ·
`LIVE_REARM_PCT` (0.05) · `LIVE_MAX_PER_HOUR` (15) · `LIVE_JUMP_PCT` · `LIVE_VOLUME_MULT` (5) ·
`LIVE_MIN_PRICE` (0.5) · `LIVE_MAX_PRICE` (20) · `LIVE_NEWS_PER_HOUR` (6) · `SIGNATURE`

Testler: `python -m unittest discover -s tests -v`
