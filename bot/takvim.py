"""Bilanço ve temettü takvimi — takip listesindeki hisselerin önümüzdeki günlerdeki olayları.

Kaynak: yfinance Ticker.calendar ("Earnings Date", "Ex-Dividend Date", "Dividend Date").
ABD için FINNHUB_KEY varsa tek istekle Finnhub bilanço takvimi de kullanılır.
Her iş günü sabah bir kez (TAKVIM_HOUR, varsayılan 09 TR) gönderilir; olay yoksa mesaj yok.
Telegram'da /takvim komutu da aynı mesajı üretir.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .common import env_int, env_str, redact, with_footer

AYLAR = ["", "Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _as_dates(value) -> list[date]:
    vals = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for v in vals:
        if v is None:
            continue
        try:
            d = v.date() if hasattr(v, "date") and not isinstance(v, date) else v
            if isinstance(d, datetime):
                d = d.date()
            if isinstance(d, date):
                out.append(d)
        except (TypeError, ValueError):
            continue
    return out


def events_for(symbol: str, display: str, cal: dict | None) -> list[tuple[date, str, str]]:
    """calendar sözlüğü → [(tarih, 'bilanço'|'temettü', görünen ad)]"""
    out = []
    if not isinstance(cal, dict):
        return out
    for d in _as_dates(cal.get("Earnings Date"))[:1]:
        out.append((d, "bilanço", display))
    for d in _as_dates(cal.get("Ex-Dividend Date")):
        out.append((d, "temettü", display))
    return out


def yahoo_calendar(symbol: str) -> dict | None:
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        return cal if isinstance(cal, dict) else None
    except Exception as e:  # noqa: BLE001
        print(redact(f"  {symbol} takvim alınamadı: {str(e)[:120]}"))
        return None


def finnhub_earnings(start: date, end: date, watch: set[str], session=None) -> list[tuple[date, str, str]]:
    key = env_str("FINNHUB_KEY")
    if not key:
        return []
    import requests
    session = session or requests
    try:
        r = session.get("https://finnhub.io/api/v1/calendar/earnings", timeout=15,
                        params={"from": str(start), "to": str(end), "token": key})
        rows = (r.json() or {}).get("earningsCalendar", []) if r.status_code == 200 else []
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Finnhub takvim hatası: {str(e)[:120]}"))
        return []
    out = []
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym in watch:
            try:
                out.append((date.fromisoformat(row["date"]), "bilanço", f"${sym}"))
            except (KeyError, ValueError):
                continue
    return out


def collect(symbols: dict[str, str], today: date, days: int = 3, calendar_fn=yahoo_calendar,
            us_watch: set[str] | None = None, session=None) -> list[tuple[date, str, str]]:
    """symbols: {yahoo_sembolü: görünen_ad}. Bugünden itibaren `days` gün içindeki olaylar."""
    end = today + timedelta(days=days)
    found = set()
    for sym, disp in symbols.items():
        for ev in events_for(sym, disp, calendar_fn(sym)):
            if today <= ev[0] <= end:
                found.add(ev)
    if us_watch:
        for ev in finnhub_earnings(today, end, us_watch, session):
            found.add(ev)
    return sorted(found)


def message(events: list[tuple[date, str, str]], today: date) -> str | None:
    if not events:
        return None
    lines = ["📅 BİLANÇO / TEMETTÜ TAKVİMİ (önümüzdeki günler)", ""]
    for kind, icon in (("bilanço", "📊 Bilanço"), ("temettü", "💰 Temettü (hak düşüm)")):
        rows = [e for e in events if e[1] == kind]
        if not rows:
            continue
        lines.append(f"{icon}:")
        for d, _, disp in rows:
            when = "bugün" if d == today else ("yarın" if d == today + timedelta(days=1) else
                                               f"{d.day} {AYLAR[d.month]}")
            lines.append(f"• {disp} — {when}")
        lines.append("")
    lines.append("Bilanço öncesi pozisyon riskini gözden geçir.")
    return with_footer(lines)


def due(now_tr: datetime, last_date: str | None) -> bool:
    """Hafta içi, TAKVIM_HOUR (09) sonrası ve bugün henüz gönderilmediyse."""
    return (now_tr.weekday() < 5 and now_tr.hour >= env_int("TAKVIM_HOUR", 9)
            and last_date != str(now_tr.date()) and env_str("TAKVIM", "1") != "0")
