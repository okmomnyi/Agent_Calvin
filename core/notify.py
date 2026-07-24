"""Outbound notification channel for AgentOS (Telegram).

A dependency-light sender over the Telegram Bot HTTP API — no python-telegram-bot
framework needed here (that arrives in Phase 8 for the full interactive bot). Used by
skills to push digests and alerts to Calvin's single authorized chat. Degrades to a
logged no-op when the bot token / chat id are not configured, so callers never crash.
"""

from __future__ import annotations

from pathlib import Path

import requests

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.notify")

_TELEGRAM_API = "https://api.telegram.org"
_MAX_LEN = 4096  # Telegram hard limit per message
_MAX_CAPTION = 1024  # Telegram hard limit for a document caption


def send_telegram(text: str, *, parse_mode: str | None = None, chat_id: str | None = None) -> bool:
    """Send a text message to the authorized Telegram chat. Returns True on success.

    Long messages are split across the 4096-char limit. Missing credentials => no-op(False).
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    target = chat_id or settings.telegram_chat_id
    if not token or not target:
        log.warning("Telegram not configured (token/chat_id missing) — message not sent.")
        return False

    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    ok = True
    for chunk in _split(text, _MAX_LEN):
        payload: dict[str, object] = {"chat_id": target, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code != 200:
                log.error("Telegram send failed %s: %s", resp.status_code, resp.text[:200])
                ok = False
        except requests.RequestException as exc:
            log.error("Telegram send error: %s", exc)
            ok = False
    return ok


def send_telegram_document(path: str, *, caption: str = "", chat_id: str | None = None) -> bool:
    """Send a file (a tailored CV, typically) to the authorized Telegram chat.

    §0's "manual step is 60 seconds" ask (#24) means Calvin needs the actual PDF in hand, not
    a filename on a server he'd have to SSH in for. A missing token/chat_id, a missing file,
    or a failed upload all degrade to a logged no-op — an application already tracked/sent
    must never be blocked on this, so callers treat the return value as best-effort.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    target = chat_id or settings.telegram_chat_id
    if not token or not target:
        log.warning("Telegram not configured (token/chat_id missing) — document not sent.")
        return False
    p = Path(path)
    if not p.is_file():
        log.warning("send_telegram_document: file not found: %s", p)
        return False

    url = f"{_TELEGRAM_API}/bot{token}/sendDocument"
    try:
        with p.open("rb") as fh:
            resp = requests.post(
                url, data={"chat_id": target, "caption": caption[:_MAX_CAPTION]},
                files={"document": (p.name, fh)}, timeout=30)
        if resp.status_code != 200:
            log.error("Telegram document send failed %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram document send error: %s", exc)
        return False


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts
