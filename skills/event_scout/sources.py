"""Event sources for the scout (Phase 14).

CTFtime (real free JSON API) plus a generic RSS/ICS source for community calendars
(Meetup group feeds, DevOpsDays pages, Luma calendars, Swahilipot, etc.) configured in
config.yaml under events.feeds. Parsers are pure functions (fixture-tested); network fetch
goes through the polite job-hunter Fetcher. Only FREE events are emitted here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.config import get_settings
from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import stable_id

log = get_logger("event_scout.sources")

CTFTIME_URL = "https://ctftime.org/api/v1/events/?limit=30"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class RawEvent:
    source: str
    external_id: str
    title: str
    fmt: str = "online"                 # online | physical
    location: str = ""
    date: str = ""                      # ISO 8601 if known
    tags: list[str] = field(default_factory=list)
    url: str = ""
    free: bool = True

    def date_epoch(self) -> float | None:
        return parse_event_date(self.date)


def parse_event_date(s: str) -> float | None:
    """Tolerant ISO date/datetime parse to epoch. Returns None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s[:len(fmt) + 6], fmt).timestamp()
        except ValueError:
            continue
    return None


def _strip(t: str) -> str:
    return _TAG_RE.sub(" ", t or "").strip()


# ------------------------------------------------------------------ CTFtime (real JSON)
def parse_ctftime(data: Any) -> list[RawEvent]:
    """CTFtime /api/v1/events → RawEvents. CTFs are free; onsite flag sets physical/online."""
    out: list[RawEvent] = []
    for e in data or []:
        onsite = bool(e.get("onsite"))
        out.append(RawEvent(
            source="ctftime", external_id=str(e.get("id") or stable_id("ctf", e.get("title", ""))),
            title=e.get("title", ""), fmt="physical" if onsite else "online",
            location=e.get("location", "") or ("onsite" if onsite else "online"),
            date=e.get("start", ""), url=e.get("url", "") or e.get("ctftime_url", ""),
            tags=["ctf", "cybersecurity", "ctf competitions"], free=True))
    return out


class CTFtimeSource:
    name = "ctftime"
    enabled = True

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    def fetch(self) -> list[RawEvent]:
        resp = self.fetcher.get(CTFTIME_URL, accept="application/json")
        if resp is None:
            return []
        try:
            return parse_ctftime(resp.json())
        except Exception:  # noqa: BLE001
            log.exception("ctftime parse failed")
            return []


# ------------------------------------------------------------------ generic RSS / ICS
def parse_rss_events(feed_text: str, source: str, tags: list[str],
                     default_fmt: str = "online") -> list[RawEvent]:
    import feedparser

    parsed = feedparser.parse(feed_text)
    out: list[RawEvent] = []
    for entry in parsed.entries:
        title = entry.get("title", "")
        url = entry.get("link", "")
        date = entry.get("published", "") or entry.get("updated", "") or entry.get("start", "")
        out.append(RawEvent(
            source=source, external_id=str(entry.get("id") or stable_id(source, url, title)),
            title=title, url=url, date=date, tags=list(tags),
            location=_strip(entry.get("location", "")), fmt=default_fmt, free=True))
    return out


def parse_ics_events(ics_text: str, source: str, tags: list[str]) -> list[RawEvent]:
    """Minimal VEVENT parser (SUMMARY/DTSTART/URL/LOCATION) — no external ICS dependency."""
    events: list[RawEvent] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, re.S):
        def field(name: str) -> str:
            m = re.search(rf"^{name}[^:]*:(.+)$", block, re.M)
            return m.group(1).strip() if m else ""
        summary = field("SUMMARY")
        if not summary:
            continue
        dt = field("DTSTART")
        iso = ""
        m = re.match(r"(\d{4})(\d{2})(\d{2})", dt)
        if m:
            iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        loc = field("LOCATION")
        events.append(RawEvent(
            source=source, external_id=stable_id(source, field("UID") or summary, dt),
            title=summary, url=field("URL"), date=iso, location=loc,
            fmt="physical" if loc else "online", tags=list(tags), free=True))
    return events


class FeedSource:
    """A configured RSS or ICS event feed (events.feeds in config.yaml)."""

    enabled = True

    def __init__(self, fetcher: Fetcher, name: str, url: str, kind: str = "rss",
                 tags: list[str] | None = None, fmt: str = "online") -> None:
        self.fetcher = fetcher
        self.name = name
        self.url = url
        self.kind = kind
        self.tags = tags or []
        self.fmt = fmt

    def fetch(self) -> list[RawEvent]:
        resp = self.fetcher.get(self.url, accept="application/rss+xml, text/calendar, text/xml")
        if resp is None:
            return []
        try:
            if self.kind == "ics":
                return parse_ics_events(resp.text, self.name, self.tags)
            return parse_rss_events(resp.text, self.name, self.tags, self.fmt)
        except Exception:  # noqa: BLE001
            log.exception("%s feed parse failed", self.name)
            return []


def build_event_sources(fetcher: Fetcher | None = None) -> list[Any]:
    fetcher = fetcher or Fetcher()
    sources: list[Any] = [CTFtimeSource(fetcher)]
    feeds = get_settings().get("events", "feeds", default={}) or {}
    for name, spec in feeds.items():
        if isinstance(spec, dict) and spec.get("url"):
            sources.append(FeedSource(fetcher, name, spec["url"], kind=spec.get("kind", "rss"),
                                      tags=spec.get("tags", []), fmt=spec.get("format", "online")))
    return [s for s in sources if getattr(s, "enabled", True)]
