"""Authoritative source gathering: search -> blocklist -> full-page fetch.

The production bug this exists to fix: essay mills (Scribd, Bartleby) were an acceptable
search hit, and that made the output WRONG, not just low-quality -- a claim synthesized
from content-farm text asserted the Camaro "is one of the last remaining muscle cars still
in production today", when GM actually retired it at the end of the 2024 model year.
Authoritative sourcing is accuracy, not polish (§0 P5).

Only full page text (not a search snippet) is ever handed to synthesis -- a snippet is a
fragment chosen by the search engine's own relevance heuristic, not evidence a fact is
actually stated on the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.logging_setup import get_logger

log = get_logger("skills.research.gather")

# Content farms / essay mills -- excluded by domain, never by keyword (a keyword filter on
# titles/snippets would just as easily exclude a legitimate source that happens to quote
# one). Config-extendable via research.blocklist_domains, never config-shrinkable: this
# list is a hard content-safety floor, not a per-deployment preference.
ESSAY_MILL_DOMAINS: frozenset[str] = frozenset({
    "scribd.com", "bartleby.com", "coursehero.com", "studymode.com",
    "termpaperwarehouse.com", "123helpme.com", "ipl.org", "gradesaver.com",
    "essaytyper.com", "papersowl.com", "studocu.com",
})

_MAX_PAGE_CHARS = 6000  # per-source cap so one huge page can't blow the LLM context budget


@dataclass
class FetchedSource:
    n: int
    title: str
    url: str
    domain: str
    text: str        # full extracted page text (falls back to the search snippet)
    snippet: str = ""


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def is_blocklisted(url: str, extra_domains: frozenset[str] = frozenset()) -> bool:
    domain = _domain(url)
    if not domain:
        return False
    blocked = ESSAY_MILL_DOMAINS | extra_domains
    return any(domain == d or domain.endswith("." + d) for d in blocked)


def search_authoritative(searcher: Any, query: str, max_results: int = 6,
                         extra_blocklist: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """Search results with every essay-mill/content-farm domain excluded. Over-fetches
    (3x) from the searcher so filtering never starves an otherwise-fine result set down to
    nothing."""
    raw = searcher.search(query, max_results=max_results * 3)
    kept = [r for r in raw if r.get("url") and not is_blocklisted(r["url"], extra_blocklist)]
    return kept[:max_results]


def extract_text(html: str) -> str:
    """Strip an HTML page down to its readable text -- script/style/nav/chrome removed,
    collapsed to one non-empty line per line of content."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    return "\n".join(lines)


def fetch_full_text(fetcher: Any, url: str, max_chars: int = _MAX_PAGE_CHARS) -> str:
    """The full page's readable text, or "" on any fetch/parse failure -- callers fall back
    to the search snippet rather than dropping the source entirely."""
    try:
        resp = fetcher.get(url, accept="text/html")
    except Exception:  # noqa: BLE001 - one source's fetch failure must not break the gather
        log.warning("research: fetching %s raised", url, exc_info=True)
        return ""
    if resp is None or resp.status_code != 200:
        return ""
    try:
        return extract_text(resp.text)[:max_chars]
    except Exception:  # noqa: BLE001 - a malformed page degrades to no full text, not a crash
        log.warning("research: extracting text from %s failed", url, exc_info=True)
        return ""


def gather_sources(searcher: Any, fetcher: Any, query: str, max_results: int = 6,
                   extra_blocklist: frozenset[str] = frozenset()) -> list[FetchedSource]:
    """Search (blocklist-filtered) -> fetch each result's full page -> degrade to the
    snippet if the fetch fails. A result with neither full text nor a snippet is dropped
    (there is nothing to cite)."""
    results = search_authoritative(searcher, query, max_results, extra_blocklist)
    sources: list[FetchedSource] = []
    for r in results:
        text = fetch_full_text(fetcher, r["url"]) or r.get("snippet", "")
        if not text:
            continue
        sources.append(FetchedSource(
            n=len(sources) + 1, title=r.get("title", ""), url=r["url"],
            domain=_domain(r["url"]), text=text, snippet=r.get("snippet", "")))
    return sources
