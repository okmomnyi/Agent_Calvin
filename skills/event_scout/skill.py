"""Event Scout skill (Phase 14).

Scrapes the event sources, keeps only FREE events, dedupes into the events table, ranks by
interest-tag match then date proximity (physical events biased to Nairobi/Mombasa), and
delivers a weekly Telegram digest plus an immediate push for anything starting within 48h.
Interest tags are editable at runtime (/tags add|remove) and "Interested" promotes an event
into the semester planner so it shows up in the morning briefing (events section).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.time_context import format_local, relative_due
from skills.event_scout.sources import build_event_sources
from skills.job_hunter.sources.base import keyword_category  # noqa: F401 (kept for parity)

log = get_logger("skills.event_scout")

_TAGS_KV = "events.tags_override"      # runtime tag edits merged over config defaults
_TOKEN_RE = __import__("re").compile(r"[a-z0-9]+")


class EventScoutSkill(BaseSkill):
    name = "event_scout"

    def __init__(self, memory: Memory | None = None, sources: list[Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._mem = memory
        self._sources = sources
        self._now = clock
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def sources(self) -> list[Any]:
        if self._sources is None:
            self._sources = build_event_sources()
        return self._sources

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "find": self.find, "scan": self.scan, "digest": self.digest,
            "tags": self.tags, "interested": self.interested, "skip": self.skip,
        }

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=["events", "notifications"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="events.scan", func=self.scan, trigger="interval", kwargs={"hours": 12}),
            ScheduledJob(id="events.digest", func=self.digest, trigger="cron",
                         kwargs={"day_of_week": "mon", "hour": 8}),
            ScheduledJob(id="events.closing", func=self.closing_soon, trigger="interval",
                         kwargs={"hours": 6}),
        ]

    # ------------------------------------------------------------- tags
    def interest_tags(self) -> list[str]:
        base = list(get_settings().get("events", "interest_tags", default=[]) or [])
        override = self.mem.kv_get(_TAGS_KV)
        if override:
            base = json.loads(override)
        return base

    def tags(self, action: str = "", tag: str = "", **_: Any) -> CommandResult:
        """/tags — list, or add/remove an interest tag (persisted)."""
        current = self.interest_tags()
        action = (action or "list").lower()
        if action == "add" and tag:
            if tag.lower() not in [t.lower() for t in current]:
                current.append(tag)
            self.mem.kv_set(_TAGS_KV, json.dumps(current))
        elif action == "remove" and tag:
            current = [t for t in current if t.lower() != tag.lower()]
            self.mem.kv_set(_TAGS_KV, json.dumps(current))
        return CommandResult(text="Interest tags: " + ", ".join(current), data={"tags": current})

    # ------------------------------------------------------------- scan (fetch + store)
    def scan(self, **_: Any) -> CommandResult:
        """Fetch all sources, keep FREE events, dedupe into the events table."""
        new = 0
        for source in self.sources:
            try:
                events = source.fetch()
            except Exception:  # noqa: BLE001
                log.exception("event source '%s' failed", getattr(source, "name", "?"))
                continue
            for ev in events:
                if not ev.free:            # paid events are DROPPED, not stored (spec)
                    continue
                if self.mem.upsert_event(ev.source, ev.external_id, title=ev.title, fmt=ev.fmt,
                                         location=ev.location, date=ev.date,
                                         tags=",".join(ev.tags), url=ev.url, free=True):
                    new += 1
        return CommandResult(text=f"Scanned events — {new} new free event(s) found.", data={"new": new})

    # ------------------------------------------------------------- ranking
    def _score(self, row: Any, tags: list[str]) -> float:
        hay = set(_TOKEN_RE.findall(f"{row['title']} {row['tags']} {row['location']}".lower()))
        tag_tokens = [set(_TOKEN_RE.findall(t.lower())) for t in tags]
        matches = sum(1 for tt in tag_tokens if tt & hay)
        score = float(matches)
        # date proximity: sooner (future) events rank higher
        from skills.event_scout.sources import parse_event_date

        epoch = parse_event_date(row["date"])
        now = self._now()
        if epoch and epoch >= now:
            score += max(0.0, 1.0 - (epoch - now) / (60 * 86400))  # within ~60 days adds up to +1
        # physical bias: penalise physical events far from Nairobi/Mombasa
        if row["format"] == "physical":
            cities = [c.lower() for c in get_settings().get("events", "physical_bias", "cities", default=[]) or []]
            loc = (row["location"] or "").lower()
            if cities and not any(c in loc for c in cities):
                score -= 1.0
        return score

    def _ranked(self, tag_filter: str = "", statuses=("new", "notified")) -> list[Any]:
        tags = self.interest_tags()
        rows: list[Any] = []
        for st in statuses:
            rows.extend(self.mem.events_by_status(st, limit=100))
        now = self._now()
        scored = []
        for r in rows:
            from skills.event_scout.sources import parse_event_date

            if any(marker in (r["title"] or "").lower()
                   for marker in ("cancelled", "canceled", "postponed")):
                continue
            epoch = parse_event_date(r["date"])
            if epoch and epoch < now - 86400:   # skip past events
                continue
            if tag_filter and tag_filter.lower() not in (
                    f"{r['title']} {r['tags']}".lower()):
                continue
            scored.append((self._score(r, tags), r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for s, r in scored if s > 0 or tag_filter]

    # ------------------------------------------------------------- find / digest
    def find(self, tag: str = "", limit: int = 10, **_: Any) -> CommandResult:
        """Answer 'any free events?' / 'any CTFs?' — ranked upcoming matches (auto-scans if empty)."""
        ranked = self._ranked(tag_filter=tag)
        if not ranked:
            self.scan()
            ranked = self._ranked(tag_filter=tag)
        if not ranked:
            return CommandResult(text="No matching free events right now.", data={"events": []})
        return CommandResult(text=self._render(ranked[:limit], tag),
                             data={"events": [self._brief(r) for r in ranked[:limit]]})

    def digest(self, notify: bool = True, **_: Any) -> CommandResult:
        """Weekly digest of new free events; marks them notified."""
        ranked = self._ranked(statuses=("new",))
        if not ranked:
            return CommandResult(text="No new free events this week.", data={"events": 0})
        text = "🎟 " + self._render(ranked[:12], "")
        if notify:
            self._notify(text)
        for r in ranked[:12]:
            self.mem.set_event_status(r["id"], "notified")
        return CommandResult(text=text, data={"events": len(ranked[:12])})

    def closing_soon(self, **_: Any) -> CommandResult:
        """Immediate push for free events starting within 48h that Calvin hasn't seen."""
        from skills.event_scout.sources import parse_event_date

        now = self._now()
        pushed = 0
        for r in self.mem.events_by_status("new", limit=100):
            epoch = parse_event_date(r["date"])
            if epoch and now <= epoch <= now + 48 * 3600:
                self._notify(f"⏰ Starting soon: {r['title']}\n{r['url']}")
                self.mem.set_event_status(r["id"], "notified")
                pushed += 1
        return CommandResult(text=f"Pushed {pushed} closing-soon event(s).", data={"pushed": pushed})

    # ------------------------------------------------------------- interested / skip
    def interested(self, event_id: int | str = 0, **_: Any) -> CommandResult:
        """Mark interested → also add to the semester planner so it shows in the briefing."""
        ev = self.mem.get_event(int(event_id))
        if not ev:
            return CommandResult(text=f"Event {event_id} not found.", ok=False)
        self.mem.set_event_status(ev["id"], "interested")
        # interest in the same dimension contradicts a "always skips X" pattern
        self._log_signal(ev, "event_skipped", contradicts=True)
        epoch = None
        from skills.event_scout.sources import parse_event_date

        epoch = parse_event_date(ev["date"])
        if epoch:
            self.mem.add_deadline(f"Event: {ev['title']}", epoch, dtype="event",
                                  weight=0.5, status="active", source="event_scout")
        return CommandResult(text=f"⭐ Added '{ev['title']}' to your planner — it'll show in briefings.")

    def skip(self, event_id: int | str = 0, **_: Any) -> CommandResult:
        ev = self.mem.get_event(int(event_id))
        self.mem.set_event_status(int(event_id), "skipped")
        self._log_signal(ev, "event_skipped")
        return CommandResult(text=f"Skipped event {event_id}.")

    def _log_signal(self, ev: Any, signal_type: str, contradicts: bool = False) -> None:
        """Passively note what Calvin skips/keeps (Phase 20). Never acts on it here."""
        if not ev:
            return
        # the discriminating dimension: the city for physical events, else the first tag
        payload = (ev["location"] or "").split(",")[0].strip() if ev["format"] == "physical"             else (ev["tags"] or "").split(",")[0].strip()
        if payload:
            self.mem.log_signal("event_scout", signal_type, payload, contradicts=contradicts,
                                now=self._now())

    # ------------------------------------------------------------- render
    def _brief(self, r: Any) -> dict[str, Any]:
        from skills.event_scout.sources import parse_event_date

        epoch = parse_event_date(r["date"])
        return {
            "id": r["id"], "title": r["title"], "format": r["format"],
            "location": r["location"], "date": r["date"], "url": r["url"],
            "tags": [t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
            "local_time": format_local(epoch) if epoch else "Date TBA",
            "starts": relative_due(epoch, self._now()).replace("due", "starts") if epoch else "time TBA",
        }

    def _render(self, rows: list[Any], tag: str) -> str:
        head = f"🎟 UPCOMING FREE EVENTS{' · ' + tag if tag else ''}"
        blocks = [head, f"{len(rows)} relevant match{'es' if len(rows) != 1 else ''}"]
        for index, row in enumerate(rows, start=1):
            event = self._brief(row)
            where = (event["location"] or "Online") if event["format"] == "physical" else "Online"
            tags = " · ".join(event["tags"][:3]) or "general"
            blocks.append(
                f"{index}. {event['title']}\n"
                f"   WHEN   {event['local_time']} · {event['starts']}\n"
                f"   WHERE  {where}\n"
                f"   TAGS   {tags}\n"
                f"   ID     {event['id']}\n"
                f"   LINK   {event['url'] or 'No link supplied'}"
            )
        blocks.append("Say “interested in event <ID>” to add one to your planner.")
        return "\n\n".join(blocks)


SKILL = EventScoutSkill()
