"""Надсилання звіту в Telegram (Bot API). Без токена — друк у лог."""
import html
import json
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


def send_report(text_html: str, files: list[tuple[str, bytes, str]] | None = None) -> bool:
    """Звіт — одним повідомленням, а всі файли — одним альбомом одразу під ним.
    files: [(ім'я, вміст, підпис)]. Підпис першого файлу стає підписом альбому."""
    if not _configured():
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не задано — звіт лише в лозі:\n%s",
                    html.unescape(text_html))
        return False
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        for part in split_message(text_html):   # зазвичай одна частина
            r = requests.post(_api("sendMessage"), timeout=30, data={
                "chat_id": chat, "text": part, "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
            r.raise_for_status()
        if files and len(files) == 1:
            name, data, caption = files[0]
            r = requests.post(_api("sendDocument"), timeout=60,
                              data={"chat_id": chat, "caption": caption},
                              files={"document": (name, data, "text/csv")})
            r.raise_for_status()
        elif files:
            media = [{"type": "document", "media": f"attach://f{n}",
                      **({"caption": files[0][2]} if n == 0 else {})} for n in range(len(files))]
            r = requests.post(_api("sendMediaGroup"), timeout=90,
                              data={"chat_id": chat, "media": json.dumps(media)},
                              files={f"f{n}": (name, data, "text/csv")
                                     for n, (name, data, _) in enumerate(files)})
            r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Не вдалось надіслати в Telegram: %s", e)
        return False


def send_alert(text: str) -> None:
    """Короткий службовий алерт (збій завантаження, зміна формату файлу)."""
    send_report("⚠️ " + html.escape(text))
