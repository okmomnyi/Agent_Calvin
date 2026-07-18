"""Shared types for job sources.

RawJob is the normalized posting every source emits; JobSource is the interface each
source implements. Keeping the network fetch (`fetch`) separate from parsing (module-
level `parse_*` functions) lets tests exercise parsers against saved fixtures offline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from typing import Any, Protocol


@dataclass
class RawJob:
    """A normalized job posting prior to scoring."""

    source: str
    external_id: str
    title: str
    company: str = ""
    url: str = ""
    description: str = ""
    location: str = ""
    tags: list[str] = field(default_factory=list)
    category_hint: str | None = None          # transcription | cloud_devops | internship | other
    apply_email: str | None = None            # if present, an email-apply path exists
    kind: str = "scrape"                      # scrape | notify_only

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "external_id": self.external_id, "title": self.title,
            "company": self.company, "url": self.url, "description": self.description[:4000],
            "location": self.location, "tags": self.tags, "category_hint": self.category_hint,
            "apply_email": self.apply_email, "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawJob:
        """Rebuild from the stored raw_json.

        The queue (Phase 26) scores a job in a WORKER process, long after the scrape that
        found it — so the posting has to be reconstructible from the database rather than
        held in memory. Unknown keys are ignored so an older stored row still loads after a
        field is added.
        """
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class JobSource(Protocol):
    """Interface for a job source. `enabled` lets a source opt out (e.g. missing key)."""

    name: str
    enabled: bool

    def fetch(self) -> list[RawJob]:
        """Return current postings from this source (network). Must not raise on empty."""
        ...


def stable_id(*parts: str) -> str:
    """Deterministic short id from arbitrary parts (for sources without their own id)."""
    digest = hashlib.sha1("|".join(p for p in parts if p).encode("utf-8")).hexdigest()
    return digest[:16]


def keyword_category(title: str, description: str = "", tags: list[str] | None = None) -> str:
    """Cheap offline pre-categorizer to hint the scorer (final category comes from the LLM)."""
    text = " ".join([title, description, " ".join(tags or [])]).lower()
    transcription = ("transcrib", "transcription", "captioning", "subtitl", "audio annotation",
                     "data annotation", "audio qc", "localization", "localisation")
    cloud = ("devops", "sre", "site reliability", "cloud engineer", "kubernetes", "docker",
             "terraform", "aws", "gcp", "azure", "ci/cd", "sysadmin", "linux", "noc",
             "infrastructure", "platform engineer", "it support")
    if any(k in text for k in transcription):
        return "transcription"
    if "intern" in text:
        return "internship"
    if any(k in text for k in cloud):
        return "cloud_devops"
    return "other"
