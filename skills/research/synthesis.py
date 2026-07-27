"""Structured document synthesis: fetched sources -> a titled, sectioned, cited plan.

Never fabricates (§0 P5): every claim in the returned plan is grounded in the numbered
SOURCES handed to the model, and the model is instructed to hedge or omit anything a
single source can't support. If the LLM is unavailable (or returns something unusable),
this degrades to a plain sourced-fact list -- never silent, never an invented narrative.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.llm import LLMClient, LLMError
from core.logging_setup import get_logger
from skills.research.gather import FetchedSource

log = get_logger("skills.research.synthesis")

# (roughly how many sections, roughly how many short paragraphs each) -- a steer for the
# model, not a hard cap; real section count still depends on what the sources support.
# Only used as a FALLBACK when the caller has no explicit page count (a plain "detailed
# report", not "10 page doc") -- see _budget_for_pages() below for the numeric case.
_SECTION_BUDGET = {"brief": (2, 2), "medium": (4, 3), "detailed": (6, 4)}
_LENGTH_TO_PAGES = {"brief": 2, "medium": 4, "detailed": 6}
_MAX_SOURCE_CHARS_IN_CONTEXT = 3000

# This house style's own body text (10.5pt Times-Roman, 15pt leading, A4 with 26mm
# top/bottom margins) runs roughly 480 words/page. A bare "detailed" bucket used to mean
# "6 sections x 4 short paragraphs" for ANY request of 5+ pages -- a 5-page ask and a
# 50-page ask got byte-for-byte the same budget AND the same fixed max_tokens=2200 output
# cap (~1.5k words, ~3 pages once JSON/heading overhead is counted) -- which is exactly why
# "make it a 10 page doc" landed at 3 pages. Both the content budget and the token
# ceiling below now scale with the actual page count asked for.
_WORDS_PER_PAGE = 480
_TOKENS_PER_PAGE = 550          # ~1.4 tokens/word for prose + JSON-structure overhead
_BASE_TOKENS = 300              # title/subtitle/abstract/table scaffolding, page-independent
_MIN_MAX_TOKENS = 1500
# A single completion call has a real ceiling -- past this, more sections just means
# thinner paragraphs, not more actual pages. Very large page counts (20+) will still
# undershoot; that's an honest limit of one-shot synthesis, not something to paper over
# with an ever-bigger number here.
_MAX_MAX_TOKENS = 4096


def _budget_for_pages(pages: int) -> tuple[int, int]:
    """(n_sections, n_paragraphs) scaled to an explicit page target, replacing the fixed
    3-bucket steer once a real number is known."""
    n_sections = max(2, min(14, round(pages * 1.0)))
    n_paras = max(2, min(5, round(pages * 0.75)))
    return n_sections, n_paras


def _max_tokens_for(pages: int) -> int:
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS, _BASE_TOKENS + pages * _TOKENS_PER_PAGE))

_SCHEMA_HINT = (
    '{"title": string, "subtitle": string, "abstract": string, '
    '"sections": [{"heading": string, "paragraphs": [string, ...]}], '
    '"table": {"title": string, "headers": [string,...], "rows": [[string,...],...]} | null}'
)


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class TableData:
    title: str
    headers: list[str]
    rows: list[list[str]]


@dataclass
class DocumentPlan:
    title: str
    subtitle: str
    abstract: str
    sections: list[Section] = field(default_factory=list)
    table: TableData | None = None
    degraded: bool = False


def _system_prompt(length: str, target_pages: int | None = None) -> str:
    if target_pages:
        n_sections, n_paras = _budget_for_pages(target_pages)
        length_line = (f"Produce about {n_sections} sections with {n_paras} short "
                       f"paragraphs each -- enough original prose for roughly "
                       f"{target_pages} PDF pages (~{target_pages * _WORDS_PER_PAGE} words) "
                       "in this report's layout.")
    else:
        n_sections, n_paras = _SECTION_BUDGET.get(length, _SECTION_BUDGET["medium"])
        length_line = f"Produce about {n_sections} sections with {n_paras} short paragraphs each."
    return (
        "You are writing an authoritative research report from the numbered SOURCES "
        "below. Every factual claim must be traceable to at least one of them -- cite "
        "inline as [n] using ONLY the given source numbers, never invent a source or a "
        "number. Never state a fact that is not present in the sources; if sources "
        "disagree, or a load-bearing detail (a date, a status, a figure) is supported by "
        "only ONE source, hedge it explicitly or omit it rather than presenting it as "
        "settled. Write ORIGINAL prose in your own words -- paraphrase, never copy "
        "sentences verbatim from a source (short quoted fragments in quotation marks, "
        f"attributed, are fine). {length_line} Include a table ONLY if the material "
        'genuinely suits one (a timeline, a comparison, specs); otherwise "table" must be null.'
    )


def synthesize(llm: LLMClient, topic: str, sources: list[FetchedSource],
              length: str = "medium", target_pages: int | None = None) -> DocumentPlan:
    """The one entry point pdf_builder.py's caller uses. Always returns a DocumentPlan --
    never raises, never blocks a report on a synthesis failure (degrades instead).

    `target_pages` (the literal "10 page doc" number, when given) scales BOTH the
    section/paragraph steer above and max_tokens below -- previously it was parsed and
    then silently discarded, so a 5-page and a 50-page request produced identical content
    capped by a fixed max_tokens=2200 (~3 pages) regardless of what was asked for.
    """
    if not sources:
        return DocumentPlan(
            title=topic.title() if topic else "Research Report",
            subtitle="No authoritative sources found",
            abstract="No verifiable authoritative sources were found for this topic, so "
                    "no findings are reported here.",
            sections=[], table=None, degraded=True,
        )

    context = "\n\n".join(
        f"[{s.n}] {s.title} ({s.domain})\n{s.text[:_MAX_SOURCE_CHARS_IN_CONTEXT]}"
        for s in sources)
    pages = target_pages or _LENGTH_TO_PAGES.get(length, _LENGTH_TO_PAGES["medium"])
    try:
        data = llm.chat_json(
            "research",
            [{"role": "system", "content": _system_prompt(length, target_pages)},
             {"role": "user", "content": f"TOPIC: {topic}\n\nSOURCES:\n{context}"}],
            schema_hint=_SCHEMA_HINT, max_tokens=_max_tokens_for(pages), temperature=0.3,
        )
    except LLMError:
        log.warning("research: synthesis failed for %r, degrading to a sourced fact list", topic)
        return _degrade_to_fact_list(topic, sources)

    sections = [
        Section(heading=str(s.get("heading", "")).strip(),
               paragraphs=[str(p) for p in (s.get("paragraphs") or []) if str(p).strip()])
        for s in (data.get("sections") or []) if str(s.get("heading", "")).strip()
    ]
    if not sections:
        log.warning("research: synthesis returned no usable sections for %r, degrading", topic)
        return _degrade_to_fact_list(topic, sources)

    table = None
    raw_table = data.get("table")
    if isinstance(raw_table, dict) and raw_table.get("rows") and raw_table.get("headers"):
        table = TableData(title=str(raw_table.get("title", "")),
                          headers=[str(h) for h in raw_table["headers"]],
                          rows=[[str(c) for c in row] for row in raw_table["rows"]])

    return DocumentPlan(
        title=str(data.get("title") or (topic.title() if topic else "Research Report")),
        subtitle=str(data.get("subtitle") or ""),
        abstract=str(data.get("abstract") or ""),
        sections=sections, table=table, degraded=False,
    )


def _degrade_to_fact_list(topic: str, sources: list[FetchedSource]) -> DocumentPlan:
    """Never a fabricated narrative when synthesis can't run: the raw sourced snippets,
    each still carrying its own [n] citation, stand on their own."""
    paragraphs = [f"{(s.snippet or s.text)[:280].strip()} [{s.n}]" for s in sources]
    return DocumentPlan(
        title=topic.title() if topic else "Research Report",
        subtitle="",
        abstract="Automated synthesis was unavailable; this report lists sourced "
                "findings directly, without narrative interpretation.",
        sections=[Section(heading="Findings", paragraphs=paragraphs)],
        table=None, degraded=True,
    )
