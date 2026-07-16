"""Modular job-source registry.

Every source implements JobSource (see base.py) and is registered in build_sources().
Adding a source is one entry here — the pipeline, scoring, and skill never change.
Parsers are pure functions (parse_* separate from network fetch) so they are fixture-tested.
"""

from __future__ import annotations

from core.config import get_settings
from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import JobSource
from skills.job_hunter.sources.jsonapi import JobicySource, RemoteOKSource, RemotiveSource
from skills.job_hunter.sources.portals import TranscriptionPortalsSource
from skills.job_hunter.sources.rss import RSSSource
from skills.job_hunter.sources.serpapi import SerpApiGoogleJobsSource
from skills.job_hunter.sources.watched import WatchedCompaniesSource

log = get_logger("job_hunter.sources")


def build_sources(fetcher: Fetcher | None = None) -> list[JobSource]:
    """Instantiate every enabled source from config. RSS feeds are data-driven."""
    settings = get_settings()
    fetcher = fetcher or Fetcher()
    sources: list[JobSource] = [
        RemoteOKSource(fetcher),
        RemotiveSource(fetcher),
        JobicySource(fetcher),
    ]

    # Generic RSS feeds — add a URL in config.yaml and it just works.
    feeds = settings.get("jobs", "rss_feeds", default={}) or {}
    for name, spec in feeds.items():
        url = spec.get("url") if isinstance(spec, dict) else spec
        category_hint = spec.get("category") if isinstance(spec, dict) else None
        if url:
            sources.append(RSSSource(fetcher, name=name, url=url, category_hint=category_hint))

    # Transcription portals (notify-only signup links)
    sources.append(TranscriptionPortalsSource())

    # Watched company careers pages (deep-crawl / diff)
    sources.append(WatchedCompaniesSource(fetcher))

    # Optional: Google Jobs via SerpAPI (only if a key is configured)
    if settings.serpapi_key:
        sources.append(SerpApiGoogleJobsSource(fetcher, settings.serpapi_key))

    enabled = [s for s in sources if s.enabled]
    log.info("Built %d job source(s): %s", len(enabled), ", ".join(s.name for s in enabled))
    return enabled
