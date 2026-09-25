"""Tüm botların paylaştığı yardımcılar: ayarlar, Telegram, Gemini, durum dosyası."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TR_TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

TELEGRAM_LIMIT = 4000  # Telegram sınırı 4096; güvenlik payı


# =============================================================================
# AYARLAR
# =============================================================================

def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"UYARI: {name}='{raw}' sayı değil, varsayılan {default} kullanılıyor")
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, default))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "evet", "on")


def dry_run() -> bool:
    """DRY_RUN=1 ise mesajlar Telegram'a gitmez, sadece ekrana yazılır."""
    return env_bool("DRY_RUN")


def require_telegram_env() -> None:
    if dry_run():
        return
    missing = [n for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env_str(n)]
    if missing:
        raise RuntimeError(
            f"Eksik ortam değişkeni: {', '.join(missing)}. "
            "GitHub'da Settings → Secrets and variables → Actions kısmına ekle."
        )


def today_tr() -> date:
    """Türkiye saatine göre bugünün tarihi (GitHub sunucusu UTC çalışır)."""
    return datetime.now(TR_TZ).date()


# =============================================================================
# GİZLİ BİLGİ SANSÜRÜ
# =============================================================================

def redact(text: str) -> str:
    """Hata mesajlarında bot token veya API anahtarı sızmasın."""
    out = str(text)
    for name in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "FINNHUB_KEY"):
        secret = os.environ.get(name, "")
        if secret and len(secret) > 6:
            out = out.replace(secret, "***")
    out = re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot***", out)
    out = re.sub(r"key=[A-Za-z0-9_-]{20,}", "key=***", out)
    out = re.sub(r"token=[A-Za-z0-9_-]{10,}", "token=***", out)
    return out


# =============================================================================
# İMZA
# =============================================================================

def footer_lines() -> list[str]:
    """Her mesajın sonuna eklenen imza.

    SIGNATURE     varsayılan "(Furkanla Katla)"
    DISCLAIMER    varsayılan boş. İstersen imzanın altına bir satır ekler (ör. "YTD").
    """
    lines = []
    sig = env_str("SIGNATURE", "(Furkanla Katla)")
    if sig and sig.lower() != "yok":
        lines.append(sig)
    disc = env_str("DISCLAIMER", "")
    if disc and disc.lower() != "yok":
        lines.append(disc)
    return lines


def with_footer(lines: list[str]) -> str:
    foot = footer_lines()
    return "\n".join(lines + ([""] + foot if foot else []))


# =============================================================================
# TELEGRAM
# =============================================================================

def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Uzun metni satır sınırlarından bölerek Telegram limitine uydurur."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # tek satır bile çok uzunsa zorla böl
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _post_telegram(chunk: str, attempts: int = 4) -> None:
    token = env_str("TELEGRAM_BOT_TOKEN")
    chat_id = env_str("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as e:
            last_error = f"ağ hatası: {type(e).__name__}"
            time.sleep(2 * attempt)
            continue

        if resp.status_code == 200:
            return
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except ValueError:
                wait = 5
            time.sleep(min(wait + 1, 60))
            last_error = "429 çok fazla istek"
            continue
        if resp.status_code >= 500:
            last_error = f"Telegram sunucu hatası {resp.status_code}"
            time.sleep(2 * attempt)
            continue

        # 400/401/403/404: tekrar denemenin faydası yok (yanlış token/chat id vb.)
        try:
            desc = resp.json().get("description", "")
        except ValueError:
            desc = resp.text[:200]
        raise RuntimeError(f"Telegram {resp.status_code}: {desc}")

    raise RuntimeError(f"Telegram gönderimi başarısız ({last_error})")


def send_telegram(text: str) -> None:
    for chunk in split_message(text):
        if dry_run():
            print("----- [DRY_RUN] Telegram mesajı -----")
            print(chunk)
            print("-------------------------------------")
            continue
        _post_telegram(chunk)
        time.sleep(1.1)  # aynı sohbete saniyede ~1 mesaj sınırı


def notify_error(bot_name: str, exc: BaseException) -> None:
    """Bot çökerse Telegram'a kısa bir uyarı gönder (gönderilemezse sessiz geç)."""
    msg = redact(f"⚠️ {bot_name} çalışırken hata verdi:\n{type(exc).__name__}: {exc}")
    print(msg, file=sys.stderr)
    try:
        send_telegram(msg[:1000])
    except Exception as e:  # noqa: BLE001 - uyarı gönderilemezse yapacak bir şey yok
        print(redact(f"Hata bildirimi de gönderilemedi: {e}"), file=sys.stderr)


def run_main(bot_name: str, func) -> None:
    """Ana fonksiyonu çalıştırır; hata olursa loglar, Telegram'a bildirir, exit 1."""
    try:
        func()
    except Exception as e:  # noqa: BLE001
        print(redact(traceback.format_exc()), file=sys.stderr)
        notify_error(bot_name, e)
        sys.exit(1)


# =============================================================================
# GEMİNİ (opsiyonel — anahtar yoksa sessizce atlanır)
# =============================================================================

_gemini_client = None
_gemini_calls = 0
_gemini_off = False


def reset_ai_budget() -> None:
    """Uzun süren canlı döngüde MAX_AI_CALLS saatlik bütçe olsun (her saat sıfırlanır)."""
    global _gemini_calls
    _gemini_calls = 0
_gemini_search_off = False


def _is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


def gemini_text(prompt: str, max_chars: int = 400, search: bool = False) -> str:
    """Gemini'den kısa metin ister. Anahtar yoksa, limit dolduysa veya hata
    olursa boş string döner — mesajın geri kalanı yine gönderilir.

    Kota dolarsa (429): önce Google aramasız dener; o da doluysa bu çalıştırmada
    Gemini'yi tamamen kapatır ve logu tek satırla bildirir (her mesajda hata basmaz)."""
    global _gemini_client, _gemini_calls, _gemini_off, _gemini_search_off

    api_key = env_str("GEMINI_API_KEY")
    if not api_key or _gemini_off:
        return ""
    if _gemini_calls >= env_int("MAX_AI_CALLS", 12):
        return ""
    use_search = search and env_str("GEMINI_SEARCH", "1") != "0" and not _gemini_search_off

    try:
        if _gemini_client is None:
            from google import genai  # geç import: anahtar yoksa paket gerekmez
            _gemini_client = genai.Client(api_key=api_key)
        _gemini_calls += 1
        kwargs = {}
        if use_search:
            # Google Search ile güncel bilgi (haber) araştırması
            from google.genai import types
            kwargs["config"] = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())])
        response = _gemini_client.models.generate_content(
            model=env_str("GEMINI_MODEL", "gemini-3.6-flash"),
            contents=prompt,
            **kwargs,
        )
        text = (getattr(response, "text", None) or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return text
    except Exception as e:  # noqa: BLE001
        if _is_quota_error(e):
            if use_search:
                _gemini_search_off = True
                print("  Gemini arama kotası doldu → bu çalıştırmada aramasız devam")
                return gemini_text(prompt, max_chars, search=False)
            _gemini_off = True
            print("  Gemini kotası doldu → bu çalıştırmada AI yorumları kapalı (mesajlar yine gider)")
            return ""
        print(redact(f"  Gemini yanıtı alınamadı: {str(e)[:200]}"))
        return ""


# =============================================================================
# DURUM DOSYASI (aynı sinyali tekrar göndermemek için)
# =============================================================================

class StateStore:
    """state/<isim>.json içinde {anahtar: {"date": "YYYY-MM-DD", ...}} tutar."""

    def __init__(self, name: str, keep_days: int = 60):
        self.path = STATE_DIR / f"{name}.json"  # testlerde STATE_DIR değiştirilebilir
        self.keep_days = keep_days
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except (ValueError, OSError):
                print(f"UYARI: {self.path.name} okunamadı, sıfırdan başlanıyor")
                self.data = {}

    def get(self, key: str) -> dict | None:
        value = self.data.get(key)
        return value if isinstance(value, dict) else None

    def set(self, key: str, **fields) -> None:
        fields.setdefault("date", today_tr().isoformat())
        self.data[key] = fields

    def days_since(self, key: str) -> int | None:
        entry = self.get(key)
        if not entry or "date" not in entry:
            return None
        try:
            return (today_tr() - date.fromisoformat(entry["date"])).days
        except ValueError:
            return None

    def prune(self) -> None:
        cutoff = today_tr() - timedelta(days=self.keep_days)
        kept = {}
        for key, value in self.data.items():
            try:
                if isinstance(value, dict) and date.fromisoformat(value.get("date", "")) >= cutoff:
                    kept[key] = value
            except ValueError:
                continue
        self.data = kept

    def save(self) -> None:
        self.prune()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)
