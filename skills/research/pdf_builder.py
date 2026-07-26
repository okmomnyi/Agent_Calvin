"""Assembles a research report PDF from a DocumentPlan + sources + an optional figure.

Layout/ordering only -- every color, font, and piece of furniture comes from
house_style.py; this module never hardcodes a look of its own (that discipline is what
makes two different-topic reports render identically).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

from skills.research import house_style as hs
from skills.research.gather import FetchedSource
from skills.research.images import ImageResult
from skills.research.synthesis import DocumentPlan

# "Roughly 5+ pages" (the brief's own words) is approximated by section count -- pagination
# isn't known until the doc is actually laid out, and a fixed section-count threshold is a
# simple, deterministic proxy that doesn't require a two-pass render.
_CONTENTS_SECTION_THRESHOLD = 5


def build_report_pdf(out_path: str | Path, topic: str, plan: DocumentPlan,
                     sources: list[FetchedSource], *, length: str = "medium",
                     image: ImageResult | None = None,
                     cover_letter_to: str | None = None) -> Path:
    """Writes the PDF and returns the path. Never raises on empty content — an
    all-degraded report (no sources, or synthesis down) still renders a real document
    rather than leaving Calvin with nothing."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    st = hs.styles()

    doc = SimpleDocTemplate(
        str(out), pagesize=A4,
        topMargin=hs.MARGIN + 6 * mm, bottomMargin=hs.MARGIN + 6 * mm,
        leftMargin=hs.MARGIN, rightMargin=hs.MARGIN,
        title=plan.title or topic or "Research Report",
        author=hs.byline(),
    )

    flow: list[Any] = []
    if cover_letter_to:
        flow += hs.cover_letter_flowables(cover_letter_to, topic)
        flow.append(PageBreak())

    flow += hs.cover_flowables(plan.title, plan.subtitle, len(sources))
    flow.append(PageBreak())

    if len(plan.sections) >= _CONTENTS_SECTION_THRESHOLD:
        flow += hs.contents_flowables([s.heading for s in plan.sections])
        flow.append(PageBreak())

    flow.append(Paragraph("Abstract", st["section_heading"]))
    flow.append(Paragraph(hs.esc(plan.abstract), st["abstract"]))

    figure_placed = False
    for i, section in enumerate(plan.sections, start=1):
        flow.append(Paragraph(f"{i}. {hs.esc(section.heading)}", st["section_heading"]))
        for para in section.paragraphs:
            flow.append(Paragraph(hs.linkify_citations(hs.esc(para)), st["body"]))
        if not figure_placed:
            # One figure per document, placed after the first section's own text -- "near
            # the relevant text" without needing a per-section image lookup.
            flow += (hs.figure_flowables(image, number=1) if image is not None
                    else hs.placeholder_figure_flowables(number=1))
            figure_placed = True

    if plan.table is not None:
        flow += hs.table_flowables(plan.table)

    flow += hs.reference_flowables(sources)

    doc.build(
        flow,
        onFirstPage=lambda c, d: hs.on_first_page(c, d, topic),
        onLaterPages=lambda c, d: hs.on_later_pages(c, d, topic),
    )
    return out
