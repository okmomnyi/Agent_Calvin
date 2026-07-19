"""Job expiry — retire what Calvin can no longer act on (Phase 34).

He asked for this twice: "when a job stays unreviewed for two days since it was listed it
should be also removed also when the deadline of the application is reached ... in this way to
prevent alot of jobs being queued and having soo much uncessarystuff in memory".

The queue had reached 83 drafted jobs. That is not a storage problem, it is an attention
problem: a list nobody can read is a list nobody reviews, and the good roles drown among the
ones that closed last week.

Two rules, both his:

* **Stale** — drafted/notified for more than `stale_days` (2) without a decision.
* **Past deadline** — the application window has closed, whatever its age.

Two things this deliberately does NOT do:

**It never deletes a row.** §0 Principle 4. Expiry sets `status='expired'`, which drops the
job out of the queue and the briefing while keeping every scrape, score, and draft. If the
rule is ever wrong, the evidence still exists; a DELETE would make a bad heuristic
unfalsifiable.

**It never expires an approved or applied job.** Those represent work already done -- a
tailored CV, a sent application, an interview that might still land. Age is meaningless once
Calvin has acted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.logging_setup import get_logger

log = get_logger("core.expiry")

DAY = 86400.0

# Statuses that are still awaiting a decision from Calvin, and so can go stale.
PENDING = ("new", "scored", "drafted", "notified")
# Statuses expiry must never touch: he has already acted on these.
PROTECTED = ("approved", "applied", "skipped", "expired")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Deadline phrasing seen in real postings. Anchored on a CUE ("deadline", "apply by",
# "closes on") rather than any date in the text -- job descriptions are full of dates that are
# not deadlines (start dates, founding years, "5 years experience since 2019"), and expiring a
# live role because it mentioned 2019 is a far worse failure than missing a deadline.
_CUE = (r"(?:deadline|apply\s+(?:by|before)|applications?\s+close|closing\s+date|"
        r"last\s+date|closes?\s+on|expires?\s+on|submit\s+by)")
_DATE_PATTERNS = [
    # 25 December 2026 / 25th Dec 2026
    re.compile(_CUE + r"\D{0,20}?(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+"
               r"(?P<m>[A-Za-z]{3,9})\.?,?\s+(?P<y>\d{4})", re.I),
    # December 25, 2026 / Dec 25 2026
    re.compile(_CUE + r"\D{0,20}?(?P<m>[A-Za-z]{3,9})\.?\s+"
               r"(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})", re.I),
    # 2026-12-25
    re.compile(_CUE + r"\D{0,20}?(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})", re.I),
    # 25/12/2026 -- day-first, which is the Kenyan convention Calvin's postings use.
    re.compile(_CUE + r"\D{0,20}?(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})", re.I),
]


def parse_deadline(text: str, *, now: float | None = None) -> float | None:
    """Best-effort application deadline as an epoch timestamp, or None.

    None is the safe answer and the common one. A job with no parsed deadline simply falls
    back to the staleness rule; a job wrongly given a past deadline vanishes immediately. So
    every ambiguity here resolves to None.
    """
    if not text:
        return None
    now = now if now is not None else time.time()
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw_month = match.group("m")
        if raw_month.isdigit():
            month = int(raw_month)
        else:
            month = _MONTHS.get(raw_month[:3].lower(), 0)
        if not 1 <= month <= 12:
            continue
        try:
            when = datetime(int(match.group("y")), month, int(match.group("d")), 23, 59)
        except ValueError:  # 31 February and friends
            continue
        stamp = when.timestamp()
        # A deadline decades away is a parse artefact, not a deadline. A deadline far in the
        # past is usually a boilerplate date; either way, don't let it drive expiry.
        if not (now - 365 * DAY) < stamp < (now + 3 * 365 * DAY):
            continue
        return stamp
    return None


@dataclass
class ExpiryResult:
    stale: list[dict[str, Any]]
    expired: list[dict[str, Any]]

    @property
    def total(self) -> int:
        return len(self.stale) + len(self.expired)

    def summary(self) -> str:
        if not self.total:
            return ""
        bits = []
        if self.expired:
            bits.append(f"{len(self.expired)} past their deadline")
        if self.stale:
            bits.append(f"{len(self.stale)} unreviewed for over 2 days")
        return f"🗂 Retired {self.total} job(s) from the queue — " + " and ".join(bits) + "."


class JobExpiry:
    """Retires jobs Calvin can no longer act on. Idempotent: re-running changes nothing."""

    def __init__(self, memory: Any, *, stale_days: float = 2.0) -> None:
        self.mem = memory
        self.stale_days = stale_days

    def backfill_deadlines(self, limit: int = 500) -> int:
        """Parse deadlines for jobs scraped before this column existed."""
        rows = self.mem.conn.execute(
            "SELECT id, raw_json, summary FROM jobs "
            "WHERE deadline IS NULL AND status = ANY(%s) LIMIT %s",
            (list(PENDING), limit)).fetchall()
        found = 0
        for row in rows:
            stamp = parse_deadline(f"{row.get('raw_json') or ''} {row.get('summary') or ''}")
            if stamp is None:
                continue
            with self.mem.tx():
                self.mem.conn.execute("UPDATE jobs SET deadline=%s WHERE id=%s",
                                      (stamp, row["id"]))
            found += 1
        return found

    def run(self, *, now: float | None = None) -> ExpiryResult:
        now = now if now is not None else time.time()
        cutoff = now - self.stale_days * DAY

        # Deadline first, so a job that is BOTH past its deadline and stale is reported under
        # the more specific reason -- that is the one that tells him the window actually shut.
        expired = self.mem.conn.execute(
            "SELECT id, title, company, deadline FROM jobs "
            "WHERE status = ANY(%s) AND deadline IS NOT NULL AND deadline < %s",
            (list(PENDING), now)).fetchall()
        stale = self.mem.conn.execute(
            "SELECT id, title, company, first_seen FROM jobs "
            "WHERE status = ANY(%s) AND first_seen < %s "
            "AND (deadline IS NULL OR deadline >= %s)",
            (list(PENDING), cutoff, now)).fetchall()

        for row in list(expired) + list(stale):
            self.mem.set_job_status(row["id"], "expired")
        if expired or stale:
            log.info("expiry: %d past deadline, %d stale", len(expired), len(stale))
        return ExpiryResult(stale=list(stale), expired=list(expired))

    def upcoming_deadlines(self, *, within_days: float = 3.0,
                           now: float | None = None) -> list[dict[str, Any]]:
        """Jobs whose window shuts soon — the other half of what he asked for.

        Expiry alone only ever delivers bad news ("that closed"). The point of tracking a
        deadline is to be told BEFORE it passes, while applying is still possible.
        """
        now = now if now is not None else time.time()
        return self.mem.conn.execute(
            "SELECT id, title, company, deadline FROM jobs "
            "WHERE status = ANY(%s) AND deadline IS NOT NULL "
            "AND deadline >= %s AND deadline <= %s ORDER BY deadline ASC",
            (list(PENDING), now, now + within_days * DAY)).fetchall()
