"""Optional Google Jobs source via SerpAPI.

Only instantiated when SERPAPI_KEY is set (has a free tier). Disabled otherwise so the
pipeline stays fully free by default. Parser is pure and fixture-testable.
"""

from __future__ import annotations

from typing import Any

from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import RawJob, keyword_category, stable_id

log = get_logger("job_hunter.sources.serpapi")

SERPAPI_URL = "https://serpapi.com/search.json"


def parse_serpapi(data: Any) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in (data or {}).get("jobs_results", []):
        title = item.get("title", "")
        company = item.get("company_name", "")
        desc = item.get("description", "")
        url = ""
        for opt in item.get("apply_options", []) or []:
            if opt.get("link"):
                url = opt["link"]
                break
        ext = item.get("job_id") or stable_id("serpapi", title, company)
        jobs.append(RawJob(
            source="google_jobs", external_id=str(ext), title=title, company=company,
            url=url, description=desc, location=item.get("location", ""),
            category_hint=keyword_category(title, desc),
        ))
    return jobs


class SerpApiGoogleJobsSource:
    name = "google_jobs"

    def __init__(self, fetcher: Fetcher, api_key: str,
                 query: str = "remote transcription OR devops OR cloud engineer") -> None:
        self.fetcher = fetcher
        self.api_key = api_key
        self.query = query

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch(self) -> list[RawJob]:
        url = f"{SERPAPI_URL}?engine=google_jobs&q={self.query}&api_key={self.api_key}"
        resp = self.fetcher.get(url, accept="application/json")
        if resp is None:
            return []
        try:
            return parse_serpapi(resp.json())
        except Exception:  # noqa: BLE001
            log.exception("serpapi: parse failed")
            return []
