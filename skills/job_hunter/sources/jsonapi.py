"""JSON-API job sources: RemoteOK, Remotive, Jobicy.

Each exposes a pure `parse_*` function (fixture-tested) and a thin source class that
fetches the endpoint through the polite Fetcher and delegates to the parser.
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import RawJob, keyword_category, stable_id

log = get_logger("job_hunter.sources.jsonapi")

REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs?count=50"


def _mkjob(source: str, ext: str, title: str, company: str, url: str, desc: str,
           location: str, tags: list[str]) -> RawJob:
    return RawJob(
        source=source, external_id=str(ext), title=title or "", company=company or "",
        url=url or "", description=desc or "", location=location or "", tags=tags or [],
        category_hint=keyword_category(title or "", desc or "", tags or []),
    )


def parse_remoteok(data: Any) -> list[RawJob]:
    """RemoteOK returns a JSON array; element 0 is a legal/marker object, skip it."""
    jobs: list[RawJob] = []
    if not isinstance(data, list):
        return jobs
    for item in data:
        if not isinstance(item, dict) or "position" not in item and "id" not in item:
            continue
        if not item.get("position") and not item.get("company"):
            continue  # the leading legal notice
        jobs.append(_mkjob(
            "remoteok", item.get("id") or stable_id(item.get("url", "")),
            item.get("position", ""), item.get("company", ""),
            item.get("url", ""), item.get("description", ""),
            item.get("location", ""), item.get("tags", []) or [],
        ))
    return jobs


def parse_remotive(data: Any) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in (data or {}).get("jobs", []):
        jobs.append(_mkjob(
            "remotive", item.get("id"), item.get("title", ""), item.get("company_name", ""),
            item.get("url", ""), item.get("description", ""),
            item.get("candidate_required_location", ""), item.get("tags", []) or [],
        ))
    return jobs


def parse_jobicy(data: Any) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in (data or {}).get("jobs", []):
        industry = item.get("jobIndustry") or []
        tags = industry if isinstance(industry, list) else [str(industry)]
        jobs.append(_mkjob(
            "jobicy", item.get("id"), item.get("jobTitle", ""), item.get("companyName", ""),
            item.get("url", ""), item.get("jobExcerpt", "") or item.get("jobDescription", ""),
            item.get("jobGeo", ""), tags,
        ))
    return jobs


class _JsonSource:
    enabled = True

    def __init__(self, fetcher: Fetcher, name: str, url: str, parser) -> None:
        self.fetcher = fetcher
        self.name = name
        self.url = url
        self._parser = parser

    def fetch(self) -> list[RawJob]:
        resp = self.fetcher.get(self.url, accept="application/json")
        if resp is None:
            return []
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            log.warning("%s: response was not JSON", self.name)
            return []
        try:
            return self._parser(data)
        except Exception:  # noqa: BLE001 - a source parse error must not break the run
            log.exception("%s: parse failed", self.name)
            return []


class RemoteOKSource(_JsonSource):
    def __init__(self, fetcher: Fetcher) -> None:
        super().__init__(fetcher, "remoteok", REMOTEOK_URL, parse_remoteok)


class RemotiveSource(_JsonSource):
    def __init__(self, fetcher: Fetcher) -> None:
        super().__init__(fetcher, "remotive", REMOTIVE_URL, parse_remotive)


class JobicySource(_JsonSource):
    def __init__(self, fetcher: Fetcher) -> None:
        super().__init__(fetcher, "jobicy", JOBICY_URL, parse_jobicy)
