"""Taranacak borsa varlıkları, gruplara ayrılmış.

Her grup ayrı saatte taranır (bkz. .github/workflows/scanner.yml), böylece
her varlık kendi piyasası kapandıktan sonra, tamamlanmış günlük mumla
değerlendirilir.

Sembol Yahoo Finance'te bulunamazsa log'a yazılır ve atlanır; bot durmaz.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

US_STOCKS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL", "ADBE",
    "AMD", "MU", "QCOM", "TXN", "INTC", "ASML", "LRCX", "AMAT", "KLAC", "MRVL",
    "CRM", "NOW", "SNOW", "PLTR", "PANW", "CRWD", "DDOG", "NET", "ZS", "MDB",
    "SOFI", "PYPL", "XYZ", "COIN", "AFRM", "UPST", "V", "MA", "AXP", "HOOD",
    "MRNA", "PFE", "LLY", "VRTX", "REGN", "ISRG", "GILD", "AMGN", "BIIB", "CRSP",
    "MARA", "RIOT", "CLSK", "OXY", "XOM", "CVX", "DVN", "FANG", "SLB",
    "COST", "WMT", "TGT", "NKE", "SBUX", "CMG", "LULU", "ABNB", "BKNG", "DIS",
    "F", "GM", "RIVN", "LCID", "CAT", "DE", "BA", "GE", "HON",
    "NFLX", "UBER", "LYFT", "DKNG", "ROKU", "SHOP", "SNAP", "PINS", "RBLX", "U",
    # Kripto ETF'leri (ABD borsasında işlem görür)
    "IBIT", "ETHA",
]

# -----------------------------------------------------------------------------
# BIST: BIST 100 hisselerinin tamamı taranır; BIST 30 / BIST 100 hisselerine ÖNCELİK (ağırlık) verilir
# (oncelik() → sinyal sırası, eşik indirimi ve canlı alarmda ayrılmış mesaj hakkı). Diğer sinyaller kapanmaz.
# Borsa İstanbul listeyi 3 ayda bir yeniler (Ocak, Nisan, Temmuz, Ekim başı).
# Bu liste: 21 Eylül 2026 duyurusu, geçerlilik 01.10.2026 - 31.12.2026.
# Yeni dönemde ya aşağıdaki listeler güncellenir ya da koda dokunmadan workflow env'ine
#   BIST100_LISTE="AEFES,AKBNK,..."  (100 hisse)   BIST30_LISTE="AEFES,..."  (30 hisse)
# yazılır. Dönem bittiyse log'a uyarı düşer (liste_eski_mi).
# -----------------------------------------------------------------------------
BIST_DONEM_BASLANGIC = date(2026, 10, 1)
BIST_DONEM_BITIS = date(2026, 12, 31)

BIST100_DONEM = [
    "AEFES", "AGHOL", "AHGAZ", "AKBNK", "AKCNS", "AKFYE", "AKSA", "AKSEN", "ALARK", "ALBRK", "ALTNY", "ANHYT",
    "ANSGR", "ARCLK", "ASELS", "ASTOR", "AYGAZ", "BERA", "BIMAS", "BINHO", "BRSAN", "BRYAT", "BSOKE", "CANTE",
    "CCOLA", "CIMSA", "CVKMD", "CWENE", "DOAS", "DOHOL", "ECILC", "ECZYT", "EGEEN", "EGGUB", "EKGYO", "ENERY",
    "ENJSA", "ENKAI", "ENTRA", "EREGL", "EUREN", "FENER", "FROTO", "GARAN", "GLRMK", "GLYHO", "GRSEL", "GUBRF",
    "GWIND", "HALKB", "HEKTS", "ISCTR", "ISDMR", "ISMEN", "KARSN", "KATMR", "KCAER", "KCHOL", "KORDS", "KRDMD",
    "LMKDC", "MAVI", "MGROS", "MPARK", "OBAMS", "ODAS", "OTKAR", "OYAKC", "PAHOL", "PETKM", "PGSUS", "RGYAS",
    "RYSAS", "SAHOL", "SASA", "SISE", "SNGYO", "SOKM", "TABGD", "TAVHL", "TCELL", "TCKRC", "THYAO", "TKFEN",
    "TOASO", "TRALT", "TRENJ", "TRGYO", "TRMET", "TSKB", "TTKOM", "TTRAK", "TUKAS", "TUPRS", "TURSG", "ULKER",
    "VAKBN", "VESTL", "YKBNK", "ZOREN",
]

BIST30_DONEM = [
    "AEFES", "AKBNK", "ASELS", "ASTOR", "BIMAS", "EKGYO", "ENKAI", "EREGL", "FROTO", "GARAN", "GUBRF", "ISCTR",
    "KCHOL", "KRDMD", "MGROS", "PETKM", "PGSUS", "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO", "TOASO",
    "TRALT", "TRMET", "TTKOM", "TUPRS", "VAKBN", "YKBNK",
]

# 1 Ekim 2026'dan önceki (Temmuz-Eylül) liste = yeni liste - girenler + çıkanlar
BIST100_GIREN = [
    "AGHOL", "AHGAZ", "AKCNS", "AKFYE", "ALBRK", "ANHYT", "AYGAZ", "BINHO", "ECZYT", "EGEEN", "EGGUB", "ENTRA",
    "GLYHO", "GWIND", "ISDMR", "KARSN", "KATMR", "KCAER", "KORDS", "LMKDC", "RGYAS", "RYSAS", "SNGYO", "TABGD",
    "TCKRC", "TRGYO", "TTRAK",
]
BIST100_CIKAN = [
    "BALSU", "BTCIM", "DAPGM", "DSTKF", "EFOR", "ESEN", "EUPWR", "GENIL", "GESAN", "GRTHO", "GSRAY", "IEYHO",
    "IZENR", "KLRHO", "KTLEV", "KUYAS", "MAGEN", "MIATK", "ODINE", "PASEU", "PATEK", "PSGYO", "QUAGR", "RALYH",
    "REEDR", "SARKY", "SKBNK",
]
BIST30_GIREN, BIST30_CIKAN = ["TRMET"], ["DSTKF"]


def _bugun_tr() -> date:
    return datetime.now(ZoneInfo("Europe/Istanbul")).date()


def _env_liste(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s for s in (p.strip().upper().removesuffix(".IS") for p in re.split(r"[,;\s]+", raw)) if s]


def bist_listeleri(today: date | None = None) -> tuple[list[str], list[str]]:
    """(BIST 100, BIST 30) hisse kodları. Env ile verilen liste her şeyi ezer; yoksa tarihe göre
    dönem listesi (1 Ekim 2026 öncesi için bir önceki dönem)."""
    today = today or _bugun_tr()
    b100, b30 = list(BIST100_DONEM), list(BIST30_DONEM)
    if today < BIST_DONEM_BASLANGIC:
        b100 = [s for s in b100 if s not in BIST100_GIREN] + BIST100_CIKAN
        b30 = [s for s in b30 if s not in BIST30_GIREN] + BIST30_CIKAN
    b100 = _env_liste("BIST100_LISTE") or b100
    b30 = _env_liste("BIST30_LISTE") or b30
    return sorted(set(b100)), sorted(set(b30))


def liste_eski_mi(today: date | None = None) -> bool:
    """Dönem bitti ve env ile yeni liste verilmediyse True (log'a uyarı yazmak için)."""
    return (today or _bugun_tr()) > BIST_DONEM_BITIS and not _env_liste("BIST100_LISTE")


def is_gyo(code: str) -> bool:
    return code.endswith(("GYO", "GY"))


BIST100, BIST30 = bist_listeleri()
# GYO'lar kendi grubunda (ayrı eşik/likidite), diğerleri 'bist' grubunda — aynı hisse iki kez taranmaz.
# BIST 100 dışındaki eski GYO listesi de taranmaya devam eder (öncelikleri düşük).
GYO_LISTESI = ["EKGYO", "ISGYO", "TRGYO", "OZKGY", "KZBGY", "SNGYO", "AKFGY"]
BIST_STOCKS = [s for s in BIST100 if not is_gyo(s)]
GYO = sorted(set(GYO_LISTESI) | {s for s in BIST100 if is_gyo(s)})


def bist_etiket(code: str) -> str:
    """'THYAO' / 'THYAO.IS' → 'BIST 30'; sadece 100'deyse 'BIST 100'; değilse ''."""
    c = code.upper().removesuffix(".IS")
    return "BIST 30" if c in BIST30 else "BIST 100" if c in BIST100 else ""


def oncelik(code: str) -> int:
    """Sinyal önceliği: BIST 30 → 2, BIST 100 → 1, diğer her şey → 0."""
    return {"BIST 30": 2, "BIST 100": 1}.get(bist_etiket(code), 0)


# BIST 100/30/50/500, tüm, banka, sınai, teknoloji, katılım endeksleri
# (Yahoo'da bulunmayan olursa log'a yazılıp atlanır)
BIST_INDEXES = ["XU100", "XU030", "XU050", "XU500", "XUTUM", "XBANK", "XUSIN", "XUTEK", "XKTUM"]

# Borsa yatırım fonları (BIST'te hisse gibi işlem görür)
BYF = {
    "ZPX30": "BIST 30 BYF (Ziraat)",
    "Z30EA": "BIST 30 Eşit Ağırlıklı BYF",
    "GLDTR": "Altın BYF (QNB)",
    "ZGOLD": "Altın BYF (Ziraat)",
    "GMSTR": "Gümüş BYF (QNB)",
    "ALTIN": "Darphane Altın Sertifikası ALTIN.S1",
}


# Yahoo sembolü -> Telegram'da görünecek isim
EMTIA_DOVIZ = {
    "GC=F": ("ALTIN (ons $)", "emtia"),
    "SI=F": ("GÜMÜŞ (ons $)", "emtia"),
    "USDTRY=X": ("DOLAR/TL", "doviz"),
    "EURTRY=X": ("EURO/TL", "doviz"),
    "GBPTRY=X": ("STERLİN/TL", "doviz"),
    "CHFTRY=X": ("İSVİÇRE FRANGI/TL", "doviz"),
    "PL=F": ("PLATİN (ons $)", "emtia"),
    "PA=F": ("PALADYUM (ons $)", "emtia"),
}

# Ons fiyatı × USD/TRY ÷ 31.1035 ile hesaplanan TL bazlı gram fiyatlar
GRAM_SERIES = {
    "GRAM_ALTIN": ("GRAM ALTIN (TL)", "GC=F"),
    "GRAM_GUMUS": ("GRAM GÜMÜŞ (TL)", "SI=F"),
}
TROY_OUNCE_GRAMS = 31.1035

CRYPTO = {
    "BTC-USD": "BITCOIN ($)",
    "ETH-USD": "ETHEREUM ($)",
    "SOL-USD": "SOLANA ($)",
    "XRP-USD": "XRP ($)",
    "BNB-USD": "BNB ($)",
    "ADA-USD": "CARDANO ($)",
    "AVAX-USD": "AVALANCHE ($)",
    "DOGE-USD": "DOGECOIN ($)",
    "LINK-USD": "CHAINLINK ($)",
}

# Piyasa filtresi için endeksler: endeks 200 günlük ortalamasının altındaysa
# o piyasadaki sinyallerde daha seçici davranılır.
BENCHMARKS = {"us": "SPY", "bist": "XU100.IS"}
METAL_BYF = {"GLDTR", "ZGOLD", "GMSTR", "ALTIN"}


def build_group(group: str) -> list[dict]:
    """Bir grubun varlık listesini döndürür.

    Her öğe: {"symbol": yahoo_sembolü, "display": mesajdaki_isim, "asset_class": sınıf}
    """
    items: list[dict] = []
    if group == "us":
        items = [{"symbol": s, "display": f"${s}", "asset_class": "hisse"} for s in US_STOCKS]
    elif group == "bist":
        if liste_eski_mi():
            print(f"  UYARI: BIST 100/30 listesinin dönemi {BIST_DONEM_BITIS} tarihinde bitti; "
                  "bot/universe.py'deki listeyi ya da BIST100_LISTE / BIST30_LISTE env'ini güncelle.")
        items = [{"symbol": f"{s}.IS", "display": f"${s}", "asset_class": "bist"} for s in BIST_STOCKS]
        items += [{"symbol": f"{s}.IS", "display": f"{s} (endeks)", "asset_class": "endeks"}
                  for s in BIST_INDEXES]
    elif group == "byf":
        items = [{"symbol": f"{s}.IS", "display": f"${s} ({name})", "asset_class": "byf"}
                 for s, name in BYF.items()]
    elif group == "gyo":
        items = [{"symbol": f"{s}.IS", "display": f"${s}", "asset_class": "gyo"} for s in GYO]
    elif group == "emtia_doviz":
        items = [{"symbol": s, "display": d, "asset_class": c} for s, (d, c) in EMTIA_DOVIZ.items()]
        items += [{"symbol": s, "display": d, "asset_class": "emtia", "synthetic": True}
                  for s, (d, _) in GRAM_SERIES.items()]
    elif group == "kripto":
        items = [{"symbol": s, "display": d, "asset_class": "kripto"} for s, d in CRYPTO.items()]
    else:
        raise ValueError(
            f"Bilinmeyen grup: {group}. Geçerli gruplar: us, bist, byf, gyo, emtia_doviz, kripto"
        )
    for it in items:
        it["group"] = group
        base = it["symbol"].removesuffix(".IS")
        if group == "us":
            it["benchmark"] = BENCHMARKS["us"]
        elif group in ("bist", "gyo") or (group == "byf" and base not in METAL_BYF):
            it["benchmark"] = BENCHMARKS["bist"]
        else:
            it["benchmark"] = None     # altın, gümüş, döviz, kripto: hisse endeksine bağlı değil
    return items


ALL_GROUPS = ["us", "bist", "byf", "gyo", "emtia_doviz", "kripto"]
