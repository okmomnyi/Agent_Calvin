"""Fetch the full posting for jobs worth applying to (Phase 34b).

The sources hand back stubs. Across Calvin's 42 pending jobs the median description was 162
characters and not one mentioned a deadline -- so deadline-based expiry had nothing to read,
and CV tailoring was matching against a headline. The real text is on the posting's own page,
which we already store a link to and never open.

Fetching is deliberately narrow:

* **Only for keepers.** One GET per job that clears the score threshold, not per scrape. A
  hunt sees hundreds of postings and keeps a handful; enriching everything would be a hundred
  times the traffic for text nobody reads.
* **Through the shared Fetcher**, so robots.txt, the 2s-per-host floor, the descriptive
  User-Agent and the backoff all apply. Politeness is inherited, not reimplemented.
* **Failure is not an error.** A dead link, a JS-only page or a robots block leaves the stub
  in place. Enrichment makes a good job better; it must never cost Calvin the posting.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.logging_setup import get_logger

log = get_logger("job_hunter.enrich")

# Page furniture that carries no signal about the role. Dropped before the text is stored so
# the description the LLM tailors against is the posting, not the site's nav and cookie bar.
_NOISE = ("script", "style", "nav", "header", "footer", "noscript", "svg", "form", "aside")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

MAX_CHARS = 12_000  # generous for a posting; a cap stops a runaway page filling the column


def html_to_text(html: str) -> str:
    """Readable text from a posting page. Returns '' rather than raising on junk input."""
    if not html or not html.strip():
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - malformed markup is normal on the open web
        return ""
    for tag in soup(_NOISE):
        tag.decompose()
    text = soup.get_text("\n")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()[:MAX_CHARS]


def fetch_description(url: str, fetcher: Any) -> str:
    """Full posting text, or '' if it can't be had. Never raises."""
    if not url:
        return ""
    try:
        resp = fetcher.get(url)
    except Exception:  # noqa: BLE001 - blocked, dead, timed out; the stub still stands
        log.info("enrich: could not fetch %s", url[:120])
        return ""
    if resp is None:
        return ""
    body = getattr(resp, "text", "") or ""
    return html_to_text(body)


def enrich_job(job_id: int, url: str, *, memory: Any, fetcher: Any) -> str:
    """Fetch the full posting and fold it into the stored job. Returns the text (or '').

    The stub is kept alongside as `description_stub`: the fetched page is the better text but
    it is scraped from an unknown template, and losing the source's own clean summary to a
    bad extraction would be a silent downgrade.
    """
    text = fetch_description(url, fetcher)
    if not text:
        return ""
    row = memory.get_job(job_id)
    if not row:
        return ""
    try:
        raw = json.loads(row.get("raw_json") or "{}") or {}
    except (ValueError, TypeError):
        raw = {}
    stub = raw.get("description") or ""
    # Only an upgrade. Some pages extract to less than the source already gave us.
    if len(text) <= len(stub):
        return ""
    raw["description_stub"] = stub
    raw["description"] = text
    raw["enriched"] = True

    from core.expiry import parse_deadline

    deadline = parse_deadline(f"{row.get('title') or ''} {text}")
    with memory.tx():
        if deadline:
            memory.conn.execute("UPDATE jobs SET raw_json=%s, deadline=%s WHERE id=%s",
                                (json.dumps(raw), deadline, job_id))
        else:
            memory.conn.execute("UPDATE jobs SET raw_json=%s WHERE id=%s",
                                (json.dumps(raw), job_id))
    log.info("enrich: job %s %d -> %d chars%s", job_id, len(stub), len(text),
             ", deadline found" if deadline else "")
    return text
