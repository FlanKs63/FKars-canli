"""Pay geri alımları — KAP'taki geri alım bildirimlerinden günlük özet (lot, ortalama fiyat, TL tutar).

Akış:
  1. KAP bildirim listesi (bot/kap.py ile aynı uç nokta) → konusu "Geri Alınan Paylara İlişkin Bildirim"
     olanlar (ya da konu/özette "geri alım" geçenler).
  2. Her bildirimin sayfası (https://www.kap.org.tr/tr/Bildirim/<no>) okunur; işlem tablosunun satırları
     ISIN kodundan (TR + 10 karakter) sonra gelen "tarih · nominal tutar · sermayeye oran · işlem fiyatı"
     sırasından ayrıştırılır. Fiyat aralık yazılmışsa ("37,30-37,50") ortası alınır (≈ işaretli).
     BIST'te 1 lot = 1 pay = 1 TL nominal, yani nominal tutar = lot sayısı.
  3. TL tutar = Σ lot × fiyat. Tablo okunamazsa şirket yine listelenir, tutar yerine KAP linki verilir.

Mesaj çok kısa: şirket · lot · TL. Önce BIST 30 / BIST 100 şirketleri (öncelik), sonra diğerleri.

Ayarlar: GERI_ALIM=0 (kapatır) · GERI_ALIM_SAAT (21) · GERI_ALIM_MAX (15 satır)
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from . import kap
from .common import env_int, redact, with_footer
from .universe import oncelik

DETAIL_URL = "https://www.kap.org.tr/tr/Bildirim/{idx}"
HEADERS = {"User-Agent": "Mozilla/5.0 (sinyal-botu)", "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu"}

ISIN_RE = re.compile(r"\bTR[A-Z0-9]{10}\b")
DATE_RE = re.compile(r"\b(\d{2})[./](\d{2})[./](\d{4})\b")
NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?"
TOKEN_RE = re.compile(rf"\s*[|;]?\s*({NUM})(?:\s*[-–]\s*({NUM}))?")


def _low(s: str) -> str:
    return (s or "").replace("İ", "i").replace("I", "ı").lower()


def is_buyback(item: dict) -> bool:
    text = _low(f"{item.get('subject', '')} {item.get('summary', '')}")
    return "geri alınan pay" in text or "geri alım" in text


def is_buyback_form(item: dict) -> bool:
    """Günlük işlem bildirimi mi (program duyurusu değil)?"""
    return "geri alınan pay" in _low(item.get("subject", ""))


def tr_num(s: str) -> float:
    """Türkçe sayı: '4.100.050' → 4100050 · '37,41' → 37.41 · '200.000,00' → 200000.0"""
    s = s.strip()
    if "," in s:
        return float(s.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d+\.\d{1,2}", s):             # '37.41' gibi ondalık nokta
        return float(s)
    return float(s.replace(".", ""))


def page_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, "html.parser").get_text(" ")
    except Exception:  # noqa: BLE001 — bs4 yoksa kaba temizlik
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def parse_rows(text: str) -> list[dict]:
    """İşlem satırları: [{'tarih': 'YYYY-MM-DD', 'lot': float, 'fiyat': float, 'yaklasik': bool}, ...]"""
    text = re.sub(r"\s+", " ", text or "")
    low = _low(text)
    i_price, i_ratio = low.find("işlem fiyat"), low.find("sermayeye oran")
    price_second = 0 <= i_price < i_ratio          # sütun sırası: fiyat oranın önündeyse
    rows: list[dict] = []
    for m in ISIN_RE.finditer(text):
        tail = text[m.end(): m.end() + 160]
        d = DATE_RE.search(tail[:60])
        if not d:
            continue
        rest = tail[d.end():]
        tokens: list[tuple[float, float | None]] = []
        pos = 0
        while len(tokens) < 3:
            t = TOKEN_RE.match(rest, pos)
            if not t:
                break
            a = tr_num(t.group(1))
            b = tr_num(t.group(2)) if t.group(2) else None
            tokens.append((a, b))
            pos = t.end()
        if len(tokens) < 2:
            continue
        lot = tokens[0][0]
        if len(tokens) >= 3:
            price_tok = tokens[1] if price_second else tokens[2]
        else:
            price_tok = tokens[1]
        lo, hi = price_tok
        price = (lo + hi) / 2 if hi else lo
        if lot < 1 or not 0 < price < 1_000_000:
            continue
        rows.append({"tarih": f"{d.group(3)}-{d.group(2)}-{d.group(1)}", "lot": lot, "fiyat": price,
                     "yaklasik": bool(hi)})
    return rows


def fetch_page(idx: str, session=None) -> str:
    import requests
    session = session or requests
    try:
        r = session.get(DETAIL_URL.format(idx=idx), headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"  KAP bildirim {idx}: HTTP {r.status_code}")
            return ""
        return page_text(r.text)
    except Exception as e:  # noqa: BLE001
        print(redact(f"  KAP bildirim {idx} alınamadı: {str(e)[:120]}"))
        return ""


def collect(now: datetime, seen: dict, list_fn=None, page_fn=None, since: str | None = None,
            pause: float = 0.4) -> list[dict]:
    """Görülmemiş geri alım bildirimlerini okuyup özetler. seen: {bildirim_no: 'YYYY-MM-DD'} güncellenir."""
    list_fn = list_fn or kap.fetch
    page_fn = page_fn or fetch_page
    out: list[dict] = []
    for item in sorted(list_fn(now, since=since), key=lambda d: int(d.get("disclosureIndex") or 0)):
        idx = str(item.get("disclosureIndex") or "")
        if not idx or idx in seen or not is_buyback(item):
            continue
        codes = kap.tickers(item)
        text = page_fn(idx)
        if pause:
            time.sleep(pause)
        rows = parse_rows(text)
        seen[idx] = str(now.date())
        if not rows and not is_buyback_form(item):
            continue                                   # büyük ihtimalle program duyurusu, işlem değil
        lot = sum(r["lot"] for r in rows)
        tutar = sum(r["lot"] * r["fiyat"] for r in rows)
        out.append({"no": idx, "kodlar": codes, "sirket": (item.get("kapTitle") or "").strip(),
                    "lot": lot, "tutar": tutar, "ort": tutar / lot if lot else 0.0,
                    "yaklasik": any(r["yaklasik"] for r in rows), "tarihler": sorted({r["tarih"] for r in rows}),
                    "okundu": bool(rows)})
    return out


def _tl_short(x: float) -> str:
    if x >= 1e9:
        return f"{x / 1e9:.2f} milyar TL".replace(".", ",")
    if x >= 1e6:
        return f"{x / 1e6:.1f} mn TL".replace(".", ",")
    return f"{x / 1e3:.0f} bin TL"


def _lot(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f} mn".replace(".", ",")
    if x >= 1e3:
        return f"{x / 1e3:.0f} bin"
    return f"{x:.0f}"


def _code(e: dict) -> str:
    return e["kodlar"][0] if e["kodlar"] else (e["sirket"][:20] or e["no"])


def _line(e: dict) -> str:
    code = _code(e)
    tag = {2: " ⭐BIST 30", 1: " BIST 100"}.get(oncelik(code), "")
    approx = "≈" if e["yaklasik"] else ""
    return f"${code}{tag} · {_lot(e['lot'])} lot · {approx}{_tl_short(e['tutar'])}"


def message(entries: list[dict], day: str) -> str | None:
    """Çok kısa özet: önce BIST 30 / BIST 100 şirketleri, sonra diğerleri; her grup TL tutara göre."""
    if not entries:
        return None
    limit = env_int("GERI_ALIM_MAX", 15)
    read = [e for e in entries if e["okundu"]]
    unread = [e for e in entries if not e["okundu"]]
    read.sort(key=lambda e: (oncelik(_code(e)) > 0, e["tutar"]), reverse=True)
    d = f"{day[8:10]}.{day[5:7]}" if len(day) == 10 else day
    lines = [f"🔁 GERİ ALIMLAR — {d}", ""]
    lines += [_line(e) for e in read[:limit]]
    if len(read) > limit:
        lines.append(f"+{len(read) - limit} şirket daha")
    if unread:
        lines.append("Tutar okunamadı: " + ", ".join(_code(e) for e in unread[:10])
                     + (" …" if len(unread) > 10 else ""))
    lines += ["", f"Toplam ≈ {_tl_short(sum(e['tutar'] for e in read))}"]
    return with_footer(lines)
