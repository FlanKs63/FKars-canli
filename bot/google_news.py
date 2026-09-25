"""Google Haberler (RSS) — Gemini kotası dolduğunda ya da anahtar yokken haber başlığı yedeği.

Anahtar gerektirmez: https://news.google.com/rss/search?q=<arama>&hl=tr&gl=TR&ceid=TR:tr
Sadece son 48 saatin (ayarlanabilir) en yeni 1-2 başlığı döner; bulunamazsa boş string.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .common import redact

URL = "https://news.google.com/rss/search"
LOCALES = {"tr": {"hl": "tr", "gl": "TR", "ceid": "TR:tr"}, "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"}}


def parse(xml_text: str) -> list[dict]:
    """RSS → [{"title", "source", "date"}] (en yeni başta)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            when = None
        if title:
            items.append({"title": title, "source": source, "date": when})
    items.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def headlines(query: str, lang: str = "tr", max_age_hours: float = 48, limit: int = 2,
              session=None, now: datetime | None = None) -> list[dict]:
    import requests
    session = session or requests
    params = {"q": f"{query} when:{max(1, int(max_age_hours // 24))}d", **LOCALES.get(lang, LOCALES["tr"])}
    try:
        r = session.get(URL, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0 (sinyal-botu)"})
        if r.status_code != 200:
            print(f"  Google Haberler HTTP {r.status_code}")
            return []
        items = parse(r.text)
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Google Haberler alınamadı: {str(e)[:150]}"))
        return []
    now = now or datetime.now(timezone.utc)
    fresh = [i for i in items if i["date"] is None or (now - i["date"]).total_seconds() <= max_age_hours * 3600]
    return fresh[:limit]


def summary(query: str, lang: str = "tr", max_chars: int = 260, **kw) -> str:
    """'Başlık (Kaynak) · Başlık (Kaynak)' — mesajlara eklenecek kısa haber satırı."""
    parts = [f"{i['title']} ({i['source']})" if i["source"] else i["title"] for i in headlines(query, lang, **kw)]
    text = " · ".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text
