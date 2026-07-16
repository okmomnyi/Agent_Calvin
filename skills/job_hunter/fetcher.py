"""Polite HTTP fetcher for the job hunter scrapers.

Enforces the spec's scraping etiquette: a descriptive User-Agent, per-host rate limiting
(default >= 2s between requests to the same host), robots.txt compliance, exponential
backoff on 429/5xx, and a hard timeout. The requests.Session and sleep/clock are
injectable so scraper tests run fully offline and without real delays.
"""

from __future__ import annotations

import time
import urllib.robotparser
from typing import Callable
from urllib.parse import urlparse

import requests

from core.logging_setup import get_logger

log = get_logger("job_hunter.fetcher")

DEFAULT_UA = "AgentOS-JobHunter/0.1 (+personal job search assistant; contact via operator)"


class Fetcher:
    """Rate-limited, robots-aware HTTP GET helper shared by all sources."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        user_agent: str = DEFAULT_UA,
        min_interval: float = 2.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self._sleep = sleep
        self._clock = clock
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ------------------------------------------------------------- robots
    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots.get(host, "__unset__")  # type: ignore[arg-type]
        if rp == "__unset__":
            rp = self._load_robots(host)
            self._robots[host] = rp
        if rp is None:  # robots unreachable => default allow (be lenient, still rate-limited)
            return True
        return rp.can_fetch(self.user_agent, url)

    def _load_robots(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self.session.get(f"{host}/robots.txt", timeout=self.timeout,
                                    headers={"User-Agent": self.user_agent})
            if resp.status_code >= 400:
                return None
            rp.parse(resp.text.splitlines())
            return rp
        except requests.RequestException:
            return None

    # ------------------------------------------------------------- rate limit
    def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        now = self._clock()
        if last is not None:
            wait = self.min_interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = self._clock()

    # ------------------------------------------------------------- get
    def get(self, url: str, *, accept: str | None = None) -> requests.Response | None:
        """GET a URL politely. Returns the Response, or None if disallowed/failed."""
        if not self._allowed(url):
            log.info("robots.txt disallows %s — skipping", url)
            return None

        host = urlparse(url).netloc
        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept

        for attempt in range(self.max_retries):
            self._throttle(host)
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
                self._sleep(min(2.0 * (2 ** attempt), 15.0))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("GET %s -> %s, backing off", url, resp.status_code)
                self._sleep(min(2.0 * (2 ** attempt), 15.0))
                continue
            return resp
        log.error("GET %s gave up after %d attempts", url, self.max_retries)
        return None
