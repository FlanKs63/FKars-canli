"""KAP (Kamuyu Aydınlatma Platformu) bildirim takibi — takip listesindeki BIST şirketlerinin önemli
açıklamaları gelince Telegram'a kısa mesaj.

Uç nokta (açık, anahtarsız):
  POST https://www.kap.org.tr/tr/api/disclosure/members/byCriteria
       {"fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD", "mkkMemberOidList": [], "subjectList": []}
  → [{publishDate, kapTitle, subject, summary?, relatedStocks, disclosureIndex, disclosureClass}, ...]
Bildirim linki: https://www.kap.org.tr/tr/Bildirim/<disclosureIndex>

Ayarlar: KAP_ALERTS=0 (kapatır) · KAP_POLL_SEC (120) · KAP_MAX_PER_HOUR (10) · KAP_ALL_SUBJECTS=1 (konu filtresi yok)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .common import env_int, env_str, redact, with_footer

URL = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
HEADERS = {"Referer": "https://www.kap.org.tr/tr/bildirim-sorgu", "User-Agent": "Mozilla/5.0 (sinyal-botu)",
           "Content-Type": "application/json", "Accept": "application/json"}

# Fiyatı etkileyebilecek konular (küçük harf, Türkçe). Rutin bildirimler (genel kurul çağrısı, bağımsız
# denetim, vb.) mesaj kalabalığı yapmasın diye alınmaz.
IMPORTANT = ("özel durum", "finansal rapor", "kar payı", "kâr payı", "sermaye artırım", "bedelsiz", "bedelli",
             "pay geri alım", "geri alım", "yeni iş ilişkisi", "sözleşme", "ihale", "birleşme", "bölünme",
             "devralma", "pay satış", "pay alım", "yatırım", "kredi derecelendirme", "iflas", "konkordato",
             "işlem yasağı", "tedbir", "borçlanma aracı", "sermaye piyasası aracı")


def _low(s: str) -> str:
    return (s or "").replace("İ", "i").replace("I", "ı").lower()


def is_important(item: dict) -> bool:
    if env_str("KAP_ALL_SUBJECTS", "0") == "1":
        return True
    text = _low(f"{item.get('subject', '')} {item.get('summary', '')}")
    return any(k in text for k in IMPORTANT)


def tickers(item: dict) -> list[str]:
    raw = item.get("relatedStocks") or item.get("stockCodes") or ""
    if isinstance(raw, list):
        raw = ",".join(map(str, raw))
    return [t for t in re.split(r"[,\s]+", str(raw).upper()) if t]


def fetch(day: datetime, session=None, since: str | None = None) -> list[dict]:
    """since: son kontrol günü (YYYY-MM-DD). Hafta sonu/bayram sonrası aradaki günler de taranır (en fazla 7 gün)."""
    import requests
    session = session or requests
    start = day.date() - timedelta(days=1)
    if since:
        try:
            start = max(min(date.fromisoformat(since), start), day.date() - timedelta(days=7))
        except ValueError:
            pass
    body = {"fromDate": str(start), "toDate": str(day.date()),
            "mkkMemberOidList": [], "subjectList": []}
    try:
        r = session.post(URL, json=body, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  KAP HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(redact(f"  KAP alınamadı: {str(e)[:150]}"))
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def message(item: dict, codes: list[str]) -> str:
    idx = item.get("disclosureIndex")
    title = (item.get("kapTitle") or "").strip()
    subject = (item.get("subject") or "").strip()
    summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
    lines = [f"📢 KAP · {' '.join('$' + c for c in codes)}", ""]
    if title:
        lines.append(title)
    if subject:
        lines.append(f"Konu: {subject}")
    if summary and summary.lower() != subject.lower():
        lines.append(f"Özet: {summary[:300]}")
    if item.get("publishDate"):
        lines.append(f"🕒 {item['publishDate']}")
    if idx:
        lines.append(f"🔗 https://www.kap.org.tr/tr/Bildirim/{idx}")
    return with_footer(lines)


def check(watch: set[str], seen, now: datetime, send, session=None, first_run: bool = False,
          sent_log: list | None = None, since: str | None = None) -> int:
    """Yeni ve önemli bildirimleri gönderir.
    seen: {disclosureIndex: 'YYYY-MM-DD'} (state; eski liste biçimi de kabul edilir). 4 günden eskiler silinir.
    sent_log: son gönderim zamanları (ISO) → gerçek 'saatte en fazla KAP_MAX_PER_HOUR' sınırı.
    Sınır dolarsa bildirim 'görüldü' sayılmaz, bir sonraki kontrolde gönderilir.
    İlk çalıştırmada (seen boş) geçmiş bildirimler gönderilmeden sadece 'görüldü' işaretlenir."""
    if isinstance(seen, list):                        # eski biçim → sözlük
        converted = {str(i): str(now.date()) for i in seen}
        seen.clear()
        seen = converted
    sent_log = sent_log if sent_log is not None else []
    items = fetch(now, session, since)
    if not items:
        return 0
    limit = env_int("KAP_MAX_PER_HOUR", 10)
    hour_ago = now - timedelta(hours=1)
    sent_log[:] = [t for t in sent_log if datetime.fromisoformat(t) > hour_ago]
    sent = 0
    for item in sorted(items, key=lambda d: int(d.get("disclosureIndex") or 0)):
        idx = str(item.get("disclosureIndex") or "")
        if not idx or idx in seen:
            continue
        codes = [c for c in tickers(item) if c in watch]
        if first_run or not codes or not is_important(item):
            seen[idx] = str(now.date())
            continue
        if len(sent_log) >= limit:
            continue                                   # sınır doldu: görüldü sayma, sonra gönderilir
        try:
            send(message(item, codes))
        except Exception as e:  # noqa: BLE001
            print(redact(f"  KAP mesajı gönderilemedi: {str(e)[:120]}"))
            continue
        seen[idx] = str(now.date())
        sent_log.append(now.isoformat())
        sent += 1
        print(f"  KAP {','.join(codes)}: {item.get('subject')}")
    cutoff = str(now.date() - timedelta(days=9))            # 7 günlük pencereden uzun tut
    for k in [k for k, d in seen.items() if d < cutoff]:
        del seen[k]
    return sent
