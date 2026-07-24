"""Himalayas internship source (#25): a dedicated feed for the one category the general
job boards structurally under-surface for a student.

Every other source in this pipeline pulls whatever mid-to-senior postings a board happens to
list and leans on scoring to sort them out afterward -- which is exactly why "every scraped
role is mid-to-senior" was the complaint. Himalayas' public JSON search API (no key, no auth,
verified live: https://himalayas.app/docs/remote-jobs-api) supports `employment_type=Intern`
as a real filter, so this source asks for internships directly instead of hoping enough of
them show up in a general feed.

Bonus over the existing RSS himalayas source (skills/job_hunter/sources/rss.py): the search
API returns the FULL job `description` (thousands of characters), not the ~168-character
logistics teaser the RSS feed gives — the exact thinness #16 traced the near-zero ATS score
to. And `expiryDate` is a real epoch timestamp, not prose to regex-guess a deadline out of.
"""

from __future__ import annotations

import re
from typing import Any

from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import RawJob

log = get_logger("job_hunter.sources.himalayas_api")

SEARCH_URL = "https://himalayas.app/jobs/api/search"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&nbsp;", " ").strip()


def parse_himalayas_internships(data: Any) -> list[RawJob]:
    """Pure parser for the search endpoint's JSON (fixture-tested)."""
    jobs: list[RawJob] = []
    for item in (data or {}).get("jobs", []):
        if item.get("employmentType") != "Intern":
            continue   # belt and braces: the query already filters, but never trust that alone
        ext = item.get("guid") or item.get("applicationLink") or ""
        if not ext:
            continue
        locations = item.get("locationRestrictions") or []
        categories = item.get("categories") or []
        jobs.append(RawJob(
            source="himalayas_internships", external_id=str(ext),
            title=item.get("title", ""), company=item.get("companyName", ""),
            url=item.get("applicationLink", ""),
            description=_strip_html(item.get("description") or item.get("excerpt") or ""),
            location=", ".join(locations) if locations else "Worldwide",
            tags=[str(c) for c in categories],
            # Known from the query, not guessed -- keyword_category() is for sources that
            # don't already know what they scraped.
            category_hint="internship",
        ))
    return jobs


class HimalayasInternshipSource:
    name = "himalayas_internships"
    enabled = True

    def __init__(self, fetcher: Fetcher, *, query: str = "", pages: int = 1) -> None:
        self.fetcher = fetcher
        self.query = query
        # One page (<=20 postings) per hunt cycle, matching every other source's single-fetch
        # shape. Himalayas' own docs say data refreshes every 24h, so polling harder than that
        # buys nothing and risks the documented 429.
        self.pages = max(1, pages)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, self.pages + 1):
            url = f"{SEARCH_URL}?employment_type=Intern&page={page}"
            if self.query:
                from urllib.parse import quote_plus

                url += f"&q={quote_plus(self.query)}"
            resp = self.fetcher.get(url, accept="application/json")
            if resp is None:
                break
            try:
                data = resp.json()
            except ValueError:
                log.warning("himalayas_internships: response was not JSON")
                break
            try:
                page_jobs = parse_himalayas_internships(data)
            except Exception:  # noqa: BLE001 - one bad page must not break the run
                log.exception("himalayas_internships: parse failed")
                break
            if not page_jobs:
                break
            jobs.extend(page_jobs)
        return jobs
