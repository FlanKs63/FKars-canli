"""Taranacak borsa varlıkları, gruplara ayrılmış.

Her grup ayrı saatte taranır (bkz. .github/workflows/scanner.yml), böylece
her varlık kendi piyasası kapandıktan sonra, tamamlanmış günlük mumla
değerlendirilir.

Sembol Yahoo Finance'te bulunamazsa log'a yazılır ve atlanır; bot durmaz.
"""

from __future__ import annotations

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

BIST_STOCKS = [
    "THYAO", "ASELS", "KCHOL", "SISE", "EREGL", "BIMAS", "TUPRS", "FROTO", "TOASO",
    "SAHOL", "PGSUS", "GARAN", "AKBNK", "ISCTR", "YKBNK", "ARCLK", "TTKOM", "TCELL",
    "PETKM", "VESTL", "ENJSA", "HEKTS", "ALARK", "ASTOR", "TAVHL", "MGROS",
    "ULKER", "SASA", "KRDMD", "OYAKC", "CCOLA", "AEFES", "DOAS", "TKFEN", "GUBRF",
]

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

GYO = ["EKGYO", "ISGYO", "TRGYO", "OZKGY", "KZBGY", "SNGYO", "AKFGY"]

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
