"""WhatsApp / SMS notifications via Africa's Talking (Phase 16).

The flip pipeline notifies Calvin here rather than on Telegram — LISTED, BUYER_FOUND, and
EXPIRED/REJECTED are time-sensitive and money-adjacent, so they go to his phone directly.

Africa's Talking exposes SMS at a stable, documented endpoint; their WhatsApp product needs
an approved sender and its endpoint is account-specific, so it is CONFIGURED rather than
hardcoded (`AT_WHATSAPP_ENDPOINT`). If WhatsApp isn't configured we fall back to SMS on the
same account, and if nothing is configured we log a no-op — a notification failure must
never break the pipeline (and must never be mistaken for a delivered message).
"""

from __future__ import annotations

import os

import requests

from core.logging_setup import get_logger

log = get_logger("core.whatsapp")

_SMS_ENDPOINT = "https://api.africastalking.com/version1/messaging"


def _creds() -> tuple[str, str, str]:
    """(username, api_key, to_number) from the environment."""
    return (os.getenv("AT_USERNAME", ""), os.getenv("AT_API_KEY", ""), os.getenv("AT_PHONE", ""))


def send_whatsapp(text: str, *, to: str | None = None) -> bool:
    """Send a WhatsApp message (falls back to SMS). Returns True only if actually delivered."""
    username, api_key, default_to = _creds()
    target = to or default_to
    if not (username and api_key and target):
        log.warning("Africa's Talking not configured (AT_USERNAME/AT_API_KEY/AT_PHONE) — "
                    "message NOT sent: %s", text[:80])
        return False

    endpoint = os.getenv("AT_WHATSAPP_ENDPOINT", "")
    sender = os.getenv("AT_WHATSAPP_FROM", "")
    if endpoint and sender:
        try:
            resp = requests.post(
                endpoint,
                headers={"apiKey": api_key, "Accept": "application/json"},
                data={"username": username, "from": sender, "to": target, "body": text},
                timeout=20)
            if resp.status_code < 300:
                return True
            log.error("WhatsApp send failed %s: %s — falling back to SMS",
                      resp.status_code, resp.text[:160])
        except requests.RequestException as exc:
            log.error("WhatsApp send error: %s — falling back to SMS", exc)
    return send_sms(text, to=target)


def send_sms(text: str, *, to: str | None = None) -> bool:
    """Send an SMS via Africa's Talking. Returns True on success."""
    username, api_key, default_to = _creds()
    target = to or default_to
    if not (username and api_key and target):
        log.warning("Africa's Talking not configured — SMS NOT sent: %s", text[:80])
        return False
    try:
        resp = requests.post(
            _SMS_ENDPOINT,
            headers={"apiKey": api_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"username": username, "to": target, "message": text[:1500]},
            timeout=20)
        if resp.status_code >= 300:
            log.error("SMS send failed %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.error("SMS send error: %s", exc)
        return False
