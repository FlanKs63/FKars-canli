"""Borsa tatilleri ve yarım günler — tatilde tarama/alarm yapılmaz, bayat veriyle mesaj gitmez.

Kaynaklar:
  NYSE  : https://ir.theice.com (NYSE Group 2026-2028 holiday calendar)
  BIST  : https://www.borsaistanbul.com/files/pay-piyasasi-2026-yili-tatil-tablosu.pdf
Dini bayramlar her yıl değişir: yeni yılın BIST tatil tablosu yayımlanınca BIST_HOLIDAYS'e eklenmeli.
Kod değiştirmeden eklemek için: EXTRA_HOLIDAYS_BIST / EXTRA_HOLIDAYS_US = "2027-03-09,2027-03-10"
"""

from __future__ import annotations

import os
from datetime import date, time as dtime

US_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
US_HALF_DAYS = {"2026-11-27": dtime(13, 0), "2026-12-24": dtime(13, 0), "2027-11-26": dtime(13, 0)}

BIST_HOLIDAYS = {
    # 2026 (resmi tablo)
    "2026-01-01", "2026-03-20", "2026-04-23", "2026-05-01", "2026-05-19",
    "2026-05-27", "2026-05-28", "2026-05-29", "2026-07-15", "2026-10-29",
    # 2027 — Diyanet takvimine göre (BIST resmi tablosu yayımlanınca kontrol edilmeli)
    "2027-01-01", "2027-03-09", "2027-03-10", "2027-03-11", "2027-04-23", "2027-05-17", "2027-05-18",
    "2027-05-19", "2027-07-15", "2027-08-30", "2027-10-29",
}
BIST_HALF_DAYS = {"2026-03-19": dtime(12, 30), "2026-05-26": dtime(12, 30), "2026-10-28": dtime(12, 30),
                  "2027-03-08": dtime(12, 30), "2027-10-28": dtime(12, 30)}


def _extra(name: str) -> set[str]:
    return {d.strip() for d in os.environ.get(name, "").split(",") if d.strip()}


def is_holiday(market: str, day: date) -> bool:
    """market: 'us' | 'bist' (bist, byf, gyo, endeks, fon aynı takvim). Kripto/emtia/döviz için hep False."""
    d = day.isoformat()
    if market in ("us", "hisse"):
        return d in US_HOLIDAYS or d in _extra("EXTRA_HOLIDAYS_US")
    if market in ("bist", "byf", "gyo", "endeks", "fon"):
        return d in BIST_HOLIDAYS or d in _extra("EXTRA_HOLIDAYS_BIST")
    return False


def is_trading_day(market: str, day: date) -> bool:
    if market in ("kripto",):
        return True
    return day.weekday() < 5 and not is_holiday(market, day)


def early_close(market: str, day: date) -> dtime | None:
    """Yarım günse kapanış saati (yerel saat), değilse None."""
    d = day.isoformat()
    if market in ("us", "hisse"):
        return US_HALF_DAYS.get(d)
    if market in ("bist", "byf", "gyo", "endeks", "fon"):
        return BIST_HALF_DAYS.get(d)
    return None
