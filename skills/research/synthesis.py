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
_SECTION_BUDGET = {"brief": (2, 2), "medium": (4, 3), "detailed": (6, 4)}
_MAX_SOURCE_CHARS_IN_CONTEXT = 3000

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


def _system_prompt(length: str) -> str:
    n_sections, n_paras = _SECTION_BUDGET.get(length, _SECTION_BUDGET["medium"])
    return (
        "You are writing an authoritative research report from the numbered SOURCES "
        "below. Every factual claim must be traceable to at least one of them -- cite "
        "inline as [n] using ONLY the given source numbers, never invent a source or a "
        "number. Never state a fact that is not present in the sources; if sources "
        "disagree, or a load-bearing detail (a date, a status, a figure) is supported by "
        "only ONE source, hedge it explicitly or omit it rather than presenting it as "
        "settled. Write ORIGINAL prose in your own words -- paraphrase, never copy "
        "sentences verbatim from a source (short quoted fragments in quotation marks, "
        f"attributed, are fine). Produce about {n_sections} sections with {n_paras} "
        "short paragraphs each. Include a table ONLY if the material genuinely suits one "
        '(a timeline, a comparison, specs); otherwise "table" must be null.'
    )


def synthesize(llm: LLMClient, topic: str, sources: list[FetchedSource],
              length: str = "medium") -> DocumentPlan:
    """The one entry point pdf_builder.py's caller uses. Always returns a DocumentPlan --
    never raises, never blocks a report on a synthesis failure (degrades instead)."""
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
    try:
        data = llm.chat_json(
            "research",
            [{"role": "system", "content": _system_prompt(length)},
             {"role": "user", "content": f"TOPIC: {topic}\n\nSOURCES:\n{context}"}],
            schema_hint=_SCHEMA_HINT, max_tokens=2200, temperature=0.3,
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
