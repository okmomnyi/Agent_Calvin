"""Watched-company deep-crawl source.

For any company Calvin names ("watch <company> careers"), the URL is stored (kv:
job_hunter.watched) and its careers page is fetched and diffed daily: job-like links
not seen before become new postings. Link extraction is a pure function (fixture-tested).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from core.logging_setup import get_logger
from core.memory import get_memory
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import RawJob, stable_id

log = get_logger("job_hunter.sources.watched")

WATCH_KEY = "job_hunter.watched"
# Links that look like a specific posting (heuristic — good enough for a daily diff).
_JOB_HREF_RE = re.compile(r"(job|career|position|opening|vacan|apply|/jobs?/)", re.I)


def extract_job_links(html: str, base_url: str, company: str) -> list[RawJob]:
    """Extract candidate posting links from a careers page (pure; fixture-tested)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    jobs: list[RawJob] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        if not (_JOB_HREF_RE.search(absolute) or _JOB_HREF_RE.search(text)):
            continue
        if not text or len(text) < 3:
            continue
        seen.add(absolute)
        jobs.append(RawJob(
            source=f"watch:{company}", external_id=stable_id(absolute),
            title=text[:200], company=company, url=absolute,
            description=f"Posting found on {company}'s careers page.",
        ))
    return jobs


class WatchedCompaniesSource:
    name = "watched_companies"

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    @property
    def enabled(self) -> bool:
        return bool(self._watched())

    @staticmethod
    def _watched() -> dict[str, str]:
        raw = get_memory().kv_get(WATCH_KEY)
        return json.loads(raw) if raw else {}

    @staticmethod
    def add(company: str, url: str) -> None:
        watched = WatchedCompaniesSource._watched()
        watched[company] = url
        get_memory().kv_set(WATCH_KEY, json.dumps(watched))
        log.info("Now watching %s careers: %s", company, url)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company, url in self._watched().items():
            resp = self.fetcher.get(url, accept="text/html")
            if resp is None:
                continue
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            try:
                jobs.extend(extract_job_links(resp.text, url or base, company))
            except Exception:  # noqa: BLE001
                log.exception("watch:%s: link extraction failed", company)
        return jobs
