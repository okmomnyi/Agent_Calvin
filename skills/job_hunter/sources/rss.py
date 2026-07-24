"""Generic RSS/Atom job source.

One class covers every RSS feed (WeWorkRemotely, WeWorkRemotely DevOps, Himalayas,
MyJobMag Kenya, BrighterMonday, CNCF, etc.) — configure feeds in config.yaml under
jobs.rss_feeds. Parsing is delegated to feedparser and normalized to RawJob.
"""

from __future__ import annotations

import re

from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import RawJob, keyword_category, stable_id

log = get_logger("job_hunter.sources.rss")

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&nbsp;", " ").strip()


def _split_company_title(entry_title: str) -> tuple[str, str]:
    """WWR-style titles are 'Company: Position'. Fall back to ('', title)."""
    if ":" in entry_title:
        company, _, position = entry_title.partition(":")
        return company.strip(), position.strip()
    return "", entry_title.strip()


def parse_feed(feed_text: str, source_name: str, category_hint: str | None = None) -> list[RawJob]:
    """Parse RSS/Atom feed text into RawJobs (pure; used directly by tests)."""
    import feedparser

    parsed = feedparser.parse(feed_text)
    jobs: list[RawJob] = []
    for entry in parsed.entries:
        raw_title = entry.get("title", "")
        company, title = _split_company_title(raw_title)
        # WWR-style feeds put it in the title ("Company: Position"); others (Himalayas
        # among them) don't, and left company permanently blank -- "[3589] DevOps Engineer
        # @ " in the digest, with nothing Calvin could judge the match against. author /
        # author_detail.name / dc:creator (feedparser's dc_creator) are where several
        # boards actually put it instead.
        if not company:
            company = (entry.get("author")
                      or (entry.get("author_detail") or {}).get("name")
                      or entry.get("dc_creator")
                      or "")
        url = entry.get("link", "")
        desc = _strip_html(entry.get("summary", "") or entry.get("description", ""))
        tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
        ext = entry.get("id") or entry.get("guid") or stable_id(source_name, url, raw_title)
        jobs.append(RawJob(
            source=source_name, external_id=str(ext), title=title, company=company,
            url=url, description=desc, tags=tags,
            category_hint=category_hint or keyword_category(title, desc, tags),
        ))
    return jobs


class RSSSource:
    enabled = True

    def __init__(self, fetcher: Fetcher, *, name: str, url: str,
                 category_hint: str | None = None) -> None:
        self.fetcher = fetcher
        self.name = name
        self.url = url
        self.category_hint = category_hint

    def fetch(self) -> list[RawJob]:
        resp = self.fetcher.get(self.url, accept="application/rss+xml, application/xml, text/xml")
        if resp is None:
            return []
        try:
            return parse_feed(resp.text, self.name, self.category_hint)
        except Exception:  # noqa: BLE001
            log.exception("%s: RSS parse failed", self.name)
            return []
