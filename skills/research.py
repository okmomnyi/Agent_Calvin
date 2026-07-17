"""Research skill (web search + fetch + synthesis with citations).

Powers "search for X" (voice/Telegram) and backs the interview prep packs. Uses a free
DuckDuckGo HTML endpoint (no API key — free-first, §0), fetches the top results through
the polite Fetcher, and synthesizes a concise, CITED answer via the research-class model.
Never fabricates sources: if search returns nothing, it says so rather than inventing.
Searcher and fetcher are injectable so synthesis/citation logic is tested offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.research")

_DDG_HTML = "https://html.duckduckgo.com/html/?q="
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Source:
    n: int
    title: str
    url: str
    snippet: str = ""


@dataclass
class ResearchResult:
    query: str
    answer: str
    sources: list[Source] = field(default_factory=list)

    def cited_text(self) -> str:
        if not self.sources:
            return self.answer
        refs = "\n".join(f"[{s.n}] {s.title} — {s.url}" for s in self.sources)
        return f"{self.answer}\n\nSources:\n{refs}"


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&nbsp;", " ").strip()


class DuckDuckGoSearcher:
    """Free DDG HTML search — returns [{title, url, snippet}]. No API key."""

    def __init__(self, fetcher: Any | None = None,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._fetcher = fetcher
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

    @property
    def fetcher(self):
        if self._fetcher is None:
            from skills.job_hunter.fetcher import Fetcher

            self._fetcher = Fetcher(respect_robots=False)  # DDG html endpoint is a search API
        return self._fetcher

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        from urllib.parse import quote_plus, unquote

        resp = self.fetcher.get(_DDG_HTML + quote_plus(query), accept="text/html")
        if resp is None:
            return []
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[dict[str, str]] = []
        for res in soup.select(".result")[: max_results * 2]:
            a = res.select_one(".result__a")
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            url = unquote(m.group(1)) if m else href
            snippet_el = res.select_one(".result__snippet")
            out.append({"title": a.get_text(" ", strip=True), "url": url,
                        "snippet": _strip_html(snippet_el.get_text(" ", strip=True)) if snippet_el else ""})
            if len(out) >= max_results:
                break
        return out


class ResearchSkill(BaseSkill):
    name = "research"

    def __init__(self, llm: LLMClient | None = None, searcher: Any | None = None) -> None:
        self._llm = llm
        self._searcher = searcher

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def searcher(self):
        if self._searcher is None:
            self._searcher = DuckDuckGoSearcher()
        return self._searcher

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"search": self.search, "research": self.search}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- core
    def research(self, query: str, *, max_results: int = 5) -> ResearchResult:
        """Search, synthesize a cited answer. Never invents sources."""
        results = self.searcher.search(query, max_results=max_results)
        if not results:
            return ResearchResult(query=query,
                                  answer="I couldn't find sources for that right now.", sources=[])
        sources = [Source(n=i + 1, title=r.get("title", ""), url=r.get("url", ""),
                          snippet=r.get("snippet", "")) for i, r in enumerate(results)]
        context = "\n".join(f"[{s.n}] {s.title}\n{s.url}\n{s.snippet}" for s in sources)
        try:
            answer = self.llm.chat(
                "research",
                [{"role": "system", "content":
                    "Answer the question from the numbered sources. Be concise and factual. Cite claims "
                    "inline as [n] using ONLY the given source numbers. Do not invent sources or facts. "
                    "If the sources don't answer it, say so."},
                 {"role": "user", "content": f"QUESTION: {query}\n\nSOURCES:\n{context}"}],
                max_tokens=600,
            )
        except LLMError:
            log.warning("research synthesis failed; returning raw snippets")
            answer = "Synthesis unavailable. Top results:\n" + "\n".join(
                f"[{s.n}] {s.snippet}" for s in sources)
        return ResearchResult(query=query, answer=answer.strip(), sources=sources)

    def search(self, query: str = "", deliver_full: bool = True, **_: Any) -> CommandResult:
        """Voice/Telegram entry: spoken-friendly answer; full cited version pushed to Telegram."""
        if not query.strip():
            return CommandResult(text="What should I look up?", ok=False)
        result = self.research(query.strip())
        if deliver_full and result.sources:
            self._notify(f"🔎 {query}\n\n{result.cited_text()}")
        return CommandResult(text=result.answer,
                             data={"sources": [s.__dict__ for s in result.sources], "query": query})


SKILL = ResearchSkill()
