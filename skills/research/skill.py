"""Research skill (Phase 39): web search + full-page fetch + cited synthesis, delivered
as a polished PDF — never inline text.

Two production bugs this rewrite fixes, both real, both observed:

1. **Routing.** "/research Camaro..." had no route at all — only "/find" reached this
   skill (see telegram_bot.py's COMMAND_MAP and core/intent.py's keyword rules, both
   updated alongside this file) — so the user fell back to a command that wasn't meant
   for this. Every phrasing that means "research this and give me a document" now reaches
   the SAME command here.
2. **Double-send** (the identical class of bug skills/job_hunter/skill.py's `approve()`
   already documents fixing): `search()` used to both push the full cited answer via
   `self._notify()` AND return it as `CommandResult.text` — arriving on Calvin's phone
   twice. Now `search()` calls `self._notify_document()` exactly ONCE (the PDF, as an
   actual file) and returns only a short confirmation as `CommandResult.text` — the two
   can never carry the same content because the document push and the text reply are no
   longer trying to deliver the same thing at all.

The output type is always a delivered document — there is no inline-text branch and no
"short answer" fast path for a research REQUEST. `research()` below is a different,
narrower thing: a quick cited-text lookup other skills (interview_prep's company
research, study_vault's web fallback) use for their OWN grounding, never shown to Calvin
as "the research report" — kept snippet-based and fast on purpose, since those callers
need an inline answer feeding into a different output, not a document of their own.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.notify import send_telegram, send_telegram_document
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from skills.research.gather import gather_sources
from skills.research.images import WikimediaImages
from skills.research.pdf_builder import build_report_pdf
from skills.research.request_parsing import parse_request
from skills.research.synthesis import synthesize

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


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "report"


class ResearchSkill(BaseSkill):
    name = "research"

    def __init__(self, llm: LLMClient | None = None, searcher: Any | None = None,
                 fetcher: Any | None = None, images: WikimediaImages | None = None,
                 notify_document: Callable[..., bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._llm = llm
        self._searcher = searcher
        self._fetcher = fetcher
        self._images = images
        # The ONLY delivery path (the double-send fix, see module docstring) -- injectable
        # so a test never reaches a real Telegram document upload.
        self._notify_document = notify_document or send_telegram_document
        self._now = clock

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

    @property
    def fetcher(self):
        if self._fetcher is None:
            from skills.job_hunter.fetcher import Fetcher

            self._fetcher = Fetcher(respect_robots=False)
        return self._fetcher

    @property
    def images(self) -> WikimediaImages:
        if self._images is None:
            self._images = WikimediaImages(fetcher=self.fetcher)
        return self._images

    def contract(self) -> SkillContract:
        """Reads `tone` and `general` — how a report is written, not what it may conclude.

        `never_invents_a_source` is the one thing no instruction may soften: a report cites
        pages that were actually fetched, and omits (never guesses) anything it can't verify.
        """
        return SkillContract(reads_categories=["tone", "general"],
                             hard_invariants=["never_invents_a_source"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"search": self.search, "research": self.search}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- internal grounding
    def research(self, query: str, *, max_results: int = 5) -> ResearchResult:
        """Quick cited-text grounding for OTHER skills' own output (see module docstring)
        — never invents sources, but deliberately NOT the authoritative-sourced, full-page-
        fetched pipeline `search()` uses below; those callers need speed, not a document.
        """
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

    # ------------------------------------------------------------- user-facing: always a PDF
    def search(self, query: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Every route into this skill — /research, /find, "look into X", "make me a doc
        about Y" — ends HERE, and this ALWAYS produces a delivered PDF. There is no
        inline-text branch and no "short answer" fast path: that choice (the agent
        answering inline instead of building the document) was the exact production bug.

        `query` carries the WHOLE utterance, directives included ("Camaro, 3-page doc") —
        `parse_request()` is the one place that gets split, so a format instruction can
        never leak into the search itself again.
        """
        parsed = parse_request(query)
        if not parsed.topic:
            return CommandResult(text="What should I research?", ok=False)

        settings = get_settings()
        max_sources = int(settings.get("research", "max_sources", default=6))
        extra_blocklist = frozenset(settings.get("research", "blocklist_domains", default=[]) or [])

        sources = gather_sources(self.searcher, self.fetcher, parsed.topic,
                                 max_results=max_sources, extra_blocklist=extra_blocklist)
        plan = synthesize(self.llm, parsed.topic, sources, length=parsed.length)

        image_dir = settings.data_dir / "research" / "images"
        image = None
        try:
            image = self.images.find_image(parsed.topic, image_dir)
        except Exception:  # noqa: BLE001 - a broken image lookup must not block the report
            log.warning("research: image lookup failed for %r", parsed.topic, exc_info=True)

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._now()))
        out_path = settings.data_dir / "research" / "reports" / f"{_slugify(parsed.topic)}-{stamp}.pdf"
        try:
            build_report_pdf(out_path, parsed.topic, plan, sources, length=parsed.length,
                             image=image, cover_letter_to=parsed.cover_letter_to)
        except Exception:  # noqa: BLE001 - a build failure must report cleanly, never crash the caller
            log.exception("research: PDF build failed for %r", parsed.topic)
            return CommandResult(
                text="I gathered sources but couldn't build the PDF — check the server logs.",
                ok=False, data={"source_count": len(sources)})

        # ONE delivery path, ONE send (the double-send fix — see module docstring): the
        # document goes out here, and ONLY here; the text reply below is a short
        # confirmation, never the report's content, so the two can never duplicate.
        if notify:
            self._notify_document(str(out_path), caption=f"📄 {plan.title}")

        confirmation = f"📄 Research report on {plan.title} sent."
        if plan.degraded:
            confirmation = f"📄 Research report on {plan.title} sent (degraded — see the PDF for details)."
        return CommandResult(
            text=confirmation, ok=bool(sources),
            data={"pdf_path": str(out_path), "title": plan.title, "degraded": plan.degraded,
                 "length": parsed.length,
                 "sources": [{"n": s.n, "title": s.title, "url": s.url, "domain": s.domain}
                            for s in sources]})


SKILL = ResearchSkill()
