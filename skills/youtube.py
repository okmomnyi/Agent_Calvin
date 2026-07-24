"""YouTube (Phase 36 Slice 3) — resolves a spoken query to a video, opened laptop-side via
web_open.py's `open_url` client action (B4). No API key needed by default: the query
resolves to a YouTube *search* URL, and YouTube itself picks the result — nothing here has
to scrape a page or guess a video id. If `YOUTUBE_API_KEY` is set, the Data API resolves the
top hit directly and opens the video page; absent the key, this falls back to search
silently. No pywhatkit, no browser automation — opening a URL is B4's job, not this one's.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable
from urllib.parse import quote_plus

import requests

from core.intent import _normalize
from core.logging_setup import get_logger
from core.skill import BaseSkill, CommandResult, SkillContract
from skills.web_open import SKILL as WEB_OPEN

log = get_logger("skills.youtube")

SEARCH_URL = "https://www.youtube.com/results?search_query={q}"
WATCH_URL = "https://www.youtube.com/watch?v={id}"

# Verbs and filler this project already strips for a similarly-shaped problem in
# skills/desktop.py's `_TRAILING` — reused in spirit rather than re-invented from scratch,
# layered on top of core/intent.py's punctuation normalization (the one the build prompt
# points at) rather than a second one.
_LEADING_VERB_RE = re.compile(
    r"^\s*(?:play|search(?: for)?|find|look up|put on)(?:\s+|$)", re.I)
_FILLER_RE = re.compile(
    r"\bon\s+you\s*tube\b|\b(please|now|for me|thanks|thank you)\b|[.!?,]", re.I)


def clean_query(text: str) -> str:
    cleaned = _normalize(text or "")
    cleaned = _LEADING_VERB_RE.sub("", cleaned)
    cleaned = _FILLER_RE.sub(" ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


class YouTubeSkill(BaseSkill):
    name = "youtube"

    def __init__(self, opener: Any = None, fetch: Callable[..., Any] | None = None) -> None:
        self._opener = opener or WEB_OPEN
        self._fetch = fetch or requests.get

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"play": self.play}

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=[])

    def play(self, query: str = "", **_: Any) -> CommandResult:
        q = clean_query(query)
        if not q:
            return CommandResult(text="What should I look for on YouTube?", ok=False)

        api_key = os.getenv("YOUTUBE_API_KEY", "")
        video_id = self._resolve_top_result(q, api_key) if api_key else None
        if video_id:
            result = self._opener.open(url=WATCH_URL.format(id=video_id))
            return CommandResult(text=f"Playing \"{q}\" on YouTube.",
                                 data=result.data, ok=result.ok)

        # No key, or the API call failed — a search page is honest; a guessed video id
        # would not be (§0 P5).
        result = self._opener.open(url=SEARCH_URL.format(q=quote_plus(q)))
        return CommandResult(text=f"Searching YouTube for \"{q}\".",
                             data=result.data, ok=result.ok)

    def _resolve_top_result(self, query: str, api_key: str) -> str | None:
        try:
            resp = self._fetch(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "id", "q": query, "type": "video", "maxResults": 1,
                       "key": api_key},
                timeout=10)
            resp.raise_for_status()
            items = resp.json().get("items") or []
            return items[0]["id"]["videoId"] if items else None
        except Exception as exc:  # noqa: BLE001 - degrade to search, never crash the turn
            log.warning("YouTube Data API lookup failed, falling back to search: %s", exc)
            return None


SKILL = YouTubeSkill()
