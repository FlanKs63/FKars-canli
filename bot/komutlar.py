"""Telegram komutları — gruptan yazılan /komutlara canlı alarm döngüsü içinden cevap verir.

  /yardim            komut listesi
  /durum             bot çalışıyor mu, açık piyasalar, takip edilen sinyaller
  /fiyat THYAO       anlık fiyat ve günlük değişim (BIST, ABD, kripto: BTC, ETH …)
  /takvim            önümüzdeki günlerin bilanço / temettü takvimi
  /sonuc             canlı alarmların son 7 gündeki sonucu (kazanma oranı, ortalama)
  /analiz THYAO      giriş / kademe / stop, kanal formatında kısa (eşik altındaysa tek satır uyarı)
  /geri              bugünün pay geri alım özeti (KAP)

Yalnız TELEGRAM_CHAT_ID'deki sohbetten gelen komutlar işlenir (başka yerden gelenler yok sayılır).
getUpdates ile çekilir (webhook gerekmez); son okunan update_id state'te tutulur.
Ayarlar: TG_COMMANDS=0 (kapatır)
"""

from __future__ import annotations

from datetime import datetime

from .common import env_str, redact, with_footer
from .signals import fmt_price

HELP = [
    "🤖 KOMUTLAR",
    "",
    "/durum — bot çalışıyor mu, takip edilen sinyaller",
    "/fiyat THYAO — anlık fiyat (BIST, ABD, BTC/ETH…)",
    "/analiz THYAO — giriş, kademe ve stop seviyeleri",
    "/geri — bugünün pay geri alımları (lot, TL)",
    "/takvim — bilanço / temettü takvimi",
    "/sonuc — canlı alarmların son 7 gün sonucu",
    "/yardim — bu liste",
]


def get_updates(offset: int, session=None) -> list[dict]:
    import requests
    session = session or requests
    token = env_str("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    try:
        r = session.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15,
                        params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'})
        data = r.json() if r.status_code == 200 else {}
    except Exception as e:  # noqa: BLE001
        print(redact(f"  Telegram komutları alınamadı: {str(e)[:150]}"))
        return []
    return data.get("result", []) if isinstance(data, dict) else []


def parse_command(update: dict, chat_id: str) -> tuple[str, list[str]] | None:
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(chat_id):
        return None
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    cmd = {"yardım": "yardim", "help": "yardim", "start": "yardim", "status": "durum", "price": "fiyat", "sonuç": "sonuc",
           "analyze": "analiz", "seviye": "analiz", "gerialim": "geri", "geri_alim": "geri",
           "gerialım": "geri"}.get(cmd, cmd)
    return cmd, parts[1:]


def resolve_symbol(query: str, bist: set[str], crypto: dict[str, str]) -> tuple[str, str]:
    """'thyao' → ('THYAO.IS', '$THYAO'); 'btc' → ('BTC-USD', 'BITCOIN ($)'); 'aapl' → ('AAPL', '$AAPL')"""
    q = query.strip().upper().replace("İ", "I").lstrip("$#")
    if q.endswith(".IS"):
        return q, f"${q[:-3]}"
    if q in bist:
        return f"{q}.IS", f"${q}"
    for sym, name in crypto.items():                  # tam eşleşme: 'ET' Ethereum'a gitmesin
        if q in (sym, sym.split("-")[0]) or q == name.upper().split()[0]:
            return sym, name
    return q, f"${q}"


def price_text(symbol: str, display: str, fetch=None) -> str:
    """Son fiyat ve günlük değişim (yfinance 2 günlük veri)."""
    try:
        if fetch is None:
            import yfinance as yf
            df = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        else:
            df = fetch(symbol)
        close = df["Close"].dropna()
    except Exception as e:  # noqa: BLE001
        return f"{display}: fiyat alınamadı ({redact(str(e))[:80]})"
    if close.empty:
        return f"{display}: veri bulunamadı (sembolü kontrol et)"
    last = float(close.iloc[-1])
    chg = (last / float(close.iloc[-2]) - 1) * 100 if len(close) > 1 else 0.0
    delay = " · ~15 dk gecikmeli" if symbol.endswith(".IS") else ""
    return f"💲 {display}: {fmt_price(last)} ({chg:+.2f}% günlük){delay}"


# /analiz için otomatik sinyal eşikleri (borsa tarayıcıyla aynı varsayılanlar; BIST 30/100'de öncelik indirimi)
ESIK = {"bist": 75.0, "gyo": 80.0, "hisse": 75.0, "kripto": 80.0}


def analiz_esigi(symbol: str, asset_class: str) -> float:
    from .common import env_float
    from .universe import oncelik
    base = ESIK.get(asset_class, 75.0)
    if asset_class in ("bist", "gyo") and symbol.endswith(".IS"):
        p = oncelik(symbol)
        base -= env_float("BIST30_AGIRLIK", 5) if p == 2 else env_float("BIST100_AGIRLIK", 3) if p == 1 else 0
    return base


def daily_frames(symbol: str) -> dict:
    from .market_data import download_many
    return download_many([symbol], period="2y")


def analiz_text(symbol: str, display: str, asset_class: str, fetch=None) -> str | None:
    """İstenen sembolün günlük analizini botun sinyal mesajı formatında döndürür. Veri yoksa None."""
    from . import signals
    from .market_data import drop_incomplete_bar
    fetch = fetch or daily_frames
    df = fetch(symbol).get(symbol)
    if df is None or df.empty or "Close" not in df.columns:
        return None
    df = drop_incomplete_bar(df, asset_class)
    sig = signals.analyze(df, symbol, display, asset_class)
    if sig is None:
        return f"{display}: analiz için yeterli geçmiş yok (en az ~1 yıllık günlük veri gerekir)"
    weak = sig.score < analiz_esigi(symbol, asset_class)
    return signals.kisa_mesaj(sig, "⚠️ Zayıf: bot bu hisseye sinyal vermezdi" if weak else "")


def status_text(now: datetime, markets: list[str], open_fn, tracker: dict) -> str:
    lines = ["✅ Bot çalışıyor", "",
             "Açık piyasalar: " + (", ".join(m for m in markets if open_fn(m, now)) or "yok (seans dışı)")]
    if tracker:
        lines.append(f"Takip edilen sinyal: {len(tracker)}")
        for sym, sig in list(tracker.items())[:10]:
            flag = " · TP1 görüldü" if getattr(sig, "tp1_hit", False) else ""
            lines.append(f"• {sig.display} giriş {fmt_price(sig.entry)} · stop {fmt_price(sig.stop)}{flag}")
    else:
        lines.append("Takip edilen açık sinyal yok.")
    return with_footer(lines)


def handle(updates: list[dict], chat_id: str, handlers: dict, send) -> int:
    """Komutları işler, cevap sayısını döndürür. handlers: {'durum': fn(args)->str, ...}"""
    n = 0
    for up in updates:
        parsed = parse_command(up, chat_id)
        if not parsed:
            continue
        cmd, args = parsed
        fn = handlers.get(cmd)
        if fn is None:
            continue
        try:
            reply = fn(args)
        except Exception as e:  # noqa: BLE001
            reply = f"Komut çalışırken hata: {redact(str(e))[:120]}"
        if reply:
            send(reply)
            n += 1
    return n


def next_offset(updates: list[dict], offset: int) -> int:
    ids = [u.get("update_id") for u in updates if isinstance(u.get("update_id"), int)]
    return max(ids) + 1 if ids else offset
