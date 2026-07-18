"""One timezone-aware clock for user-facing dates, deadlines, and model context."""

from __future__ import annotations

import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from core.config import get_settings


def user_zone() -> ZoneInfo:
    """Return the configured user timezone, falling back safely if it is invalid."""
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Africa/Nairobi")


def local_now(epoch: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, user_zone())


def greeting(epoch: float | None = None) -> str:
    hour = local_now(epoch).hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def start_of_local_day(epoch: float | None = None) -> float:
    now = local_now(epoch)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# How AgentOS talks, everywhere. One voice for the whole system.
VOICE = """\
Speak like a capable colleague, not a command line. Warm, direct, plain English. \
Contractions are fine. Lead with the answer, then the detail that earns it. \
Explain your reasoning briefly when it isn't obvious, and skip it when it is. \
Keep it short — a couple of sentences unless real detail is genuinely needed; \
never pad, never repeat the question back, never open with "Certainly" or "Sure thing". \
If you couldn't do something, say so plainly and say why. \
If you're unsure, say that too — an honest "I don't know" is worth more than a confident guess."""


def runtime_truth(epoch: float | None = None) -> str:
    now = local_now(epoch)
    return (
        f"Current user-local date and time: {now.strftime('%A, %d %B %Y at %H:%M')} "
        f"({now.tzname()}, timezone {get_settings().tz}). "
        "Use this for today/tomorrow, greetings, schedules, deadlines, and relative dates. "
        "Never assume it is morning. Never invent facts, dates, IDs, links, or completed actions; "
        "if supplied evidence is insufficient, say what is unknown.\n\n"
        # Applied to EVERY generative call, so the whole system speaks with one voice rather
        # than each skill inventing its own tone. Adapted from the jarvis reference's
        # "conversational and concise, explain your reasoning simply" framing -- AgentOS was
        # answering like a CLI printing output.
        + VOICE
    )


def parse_local_datetime(value: str, *, date_at_end_of_day: bool = True) -> float | None:
    """Parse ISO date/time in the user's timezone; a bare date means its end, not midnight."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if len(raw) == 10 and date_at_end_of_day:
            parsed = datetime.combine(parsed.date(), dt_time(23, 59, 59))
        parsed = parsed.replace(tzinfo=user_zone())
    return parsed.timestamp()


def format_local(epoch: float, *, include_time: bool = True) -> str:
    fmt = "%a %d %b, %H:%M %Z" if include_time else "%a %d %b"
    return local_now(epoch).strftime(fmt)


def relative_due(epoch: float, now: float | None = None) -> str:
    current = time.time() if now is None else now
    seconds = epoch - current
    absolute = abs(seconds)
    if seconds < 0:
        if absolute < 3600:
            return f"OVERDUE by {max(1, round(absolute / 60))} min"
        if absolute < 86400:
            return f"OVERDUE by {absolute / 3600:.1f}h"
        return f"OVERDUE by {absolute / 86400:.1f}d"
    if seconds < 3600:
        return f"due in {max(1, round(seconds / 60))} min"
    if seconds < 86400:
        return f"due in {seconds / 3600:.1f}h"
    if local_now(epoch).date() == local_now(current + 86400).date():
        return "due tomorrow"
    return f"due in {seconds / 86400:.1f}d"
