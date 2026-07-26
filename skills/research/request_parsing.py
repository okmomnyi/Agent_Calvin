"""Split a research request into its TOPIC and its format/length/extras directives.

The production bug this exists to fix: "Camaro and make a 3page doc" was searched
LITERALLY — the format instruction leaked into the search query because nothing ever
separated "what to research" from "how to deliver it". `parse_request()` is the one place
that split happens; every route into skills/research (/research, /find, a keyword-routed
"make me a doc about X") funnels through it exactly once.

Reuses core.intent's own punctuation normalizer (`_normalize` — Whisper's curly-quote
inconsistency) rather than re-solving that problem a second time here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.intent import _normalize as normalize_text

# "3-page", "3 page", "3pages" -- a leading hyphen and/or spaces are both optional so all
# three spoken/typed variants match the same group.
_PAGE_COUNT_RE = re.compile(r"\b(\d+)\s*-?\s*pages?\b", re.I)
_BRIEF_RE = re.compile(r"\bbrief\b|\bshort\b", re.I)
_DETAILED_RE = re.compile(r"\bdetailed\b|\bcomprehensive\b|\bin-?depth\b|\blong\b", re.I)
_COVER_LETTER_RE = re.compile(
    r"\bwith\s+a\s+cover\s+letter\b(?:\s+(?:to|for|addressed\s+to)\s+(?P<who>[^,.]+))?", re.I)

# The "make/write/create/build (me) a doc/report/pdf/paper (about/on/for) TOPIC" PREFIX
# form -- the deliverable-type noun comes before the topic, so this is matched and
# stripped wherever it appears (it always precedes real content, never follows it).
_DOC_FRAMING_PREFIX_RE = re.compile(
    r"\b(?:make|write|create|build)\b\s*(?:me\s+)?(?:a\s+)?(?:doc(?:ument)?|report|pdf|paper)\b"
    r"(?:\s+(?:about|on|for))?\s*", re.I)
# The "TOPIC, 3-page doc" / "TOPIC and make a doc" SUFFIX form -- the noun (and, if
# present, its dangling verb) trails the real topic, so both are anchored to the END of
# the string only. An un-anchored strip would just as happily eat "report" out of "the
# Mueller report" or "annual reports" wherever it appeared in the topic itself.
_TRAILING_DOC_NOUN_RE = re.compile(
    r"\s*\b(?:a\s+|the\s+)?(?:doc(?:ument)?|report|pdf|paper)\b\s*$", re.I)
_TRAILING_VERB_RE = re.compile(r"\s*\b(?:make|write|create|build)\b\s*(?:me\s*)?$", re.I)

Length = str  # "brief" | "medium" | "detailed"


@dataclass
class ParsedRequest:
    topic: str
    length: Length
    target_pages: int | None
    cover_letter_to: str | None


def parse_request(text: str) -> ParsedRequest:
    """Never returns an empty topic for non-empty input unless the WHOLE input was format
    directives with nothing else — callers treat that as "no topic given", same as today's
    blank-query handling."""
    cleaned = normalize_text(text)

    cover_letter_to: str | None = None
    m = _COVER_LETTER_RE.search(cleaned)
    if m:
        who = (m.group("who") or "").strip()
        cover_letter_to = who or "the hiring team"
        cleaned = cleaned[: m.start()] + cleaned[m.end() :]

    target_pages: int | None = None
    page_m = _PAGE_COUNT_RE.search(cleaned)
    if page_m:
        target_pages = int(page_m.group(1))
        cleaned = cleaned[: page_m.start()] + cleaned[page_m.end() :]

    length: Length
    if target_pages is not None:
        length = "brief" if target_pages <= 2 else "detailed" if target_pages >= 5 else "medium"
    elif _BRIEF_RE.search(cleaned):
        length = "brief"
        cleaned = _BRIEF_RE.sub("", cleaned)
    elif _DETAILED_RE.search(cleaned):
        length = "detailed"
        cleaned = _DETAILED_RE.sub("", cleaned)
    else:
        length = "medium"

    cleaned = _DOC_FRAMING_PREFIX_RE.sub("", cleaned)
    cleaned = _TRAILING_DOC_NOUN_RE.sub("", cleaned)
    cleaned = _TRAILING_VERB_RE.sub("", cleaned)
    cleaned = re.sub(r"[,;]+", " ", cleaned)
    topic = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.-")

    return ParsedRequest(topic=topic, length=length, target_pages=target_pages,
                         cover_letter_to=cover_letter_to)
