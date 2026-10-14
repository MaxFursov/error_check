"""Надсилання звіту в Telegram (Bot API). Без токена — друк у лог."""
import html
import logging
import os

import requests

log = logging.getLogger(__name__)
MAX_LEN = 3900  # ліміт Telegram — 4096


def _configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Ділить по рядках, щоб не різати HTML-теги посередині."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        parts.append(cur)
    return parts


def send_report(text_html: str, csv_bytes: bytes | None = None, csv_name: str = "report.csv") -> bool:
    if not _configured():
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не задано — звіт лише в лозі:\n%s",
                    html.unescape(text_html))
        return False
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        for part in split_message(text_html):
            r = requests.post(_api("sendMessage"), timeout=30, data={
                "chat_id": chat, "text": part, "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
            r.raise_for_status()
        if csv_bytes:
            r = requests.post(_api("sendDocument"), timeout=60, data={"chat_id": chat},
                              files={"document": (csv_name, csv_bytes, "text/csv")})
            r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Не вдалось надіслати в Telegram: %s", e)
        return False


def send_alert(text: str) -> None:
    """Короткий службовий алерт (збій завантаження, зміна формату файлу)."""
    send_report("⚠️ " + html.escape(text))
