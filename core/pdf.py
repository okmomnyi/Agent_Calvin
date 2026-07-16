"""PDF generation for AgentOS (ReportLab).

A single build_pdf() helper producing clean, neutral/charcoal documents (no blue —
per the design intent for prep packs, lecture notes, and mock papers). Used by the
interview prep skill (Phase 6) and later the lecture pipeline / semester planner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

# Charcoal palette — deliberately no blue.
_CHARCOAL = colors.HexColor("#1f2328")
_SLATE = colors.HexColor("#3c4650")
_RULE = colors.HexColor("#c9ced4")


def _styles():
    base = getSampleStyleSheet()
    title = ParagraphStyle("AOSTitle", parent=base["Title"], textColor=_CHARCOAL,
                           fontName="Helvetica-Bold", fontSize=20, spaceAfter=4, alignment=TA_LEFT)
    subtitle = ParagraphStyle("AOSSub", parent=base["Normal"], textColor=_SLATE,
                              fontSize=10, spaceAfter=14)
    heading = ParagraphStyle("AOSHeading", parent=base["Heading2"], textColor=_CHARCOAL,
                             fontName="Helvetica-Bold", fontSize=13, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("AOSBody", parent=base["Normal"], textColor=_CHARCOAL,
                          fontSize=10.5, leading=15, spaceAfter=6)
    return title, subtitle, heading, body


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_pdf(
    out_path: str | Path,
    title: str,
    sections: Iterable[tuple[str, Iterable[str]]],
    subtitle: str = "",
) -> Path:
    """Write a PDF with a title, optional subtitle, and (heading, [paragraphs]) sections.

    Paragraphs starting with '- ' render as bullet-ish lines. Returns the output path.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    title_s, sub_s, head_s, body_s = _styles()

    doc = SimpleDocTemplate(str(out), pagesize=A4, topMargin=20 * mm, bottomMargin=18 * mm,
                            leftMargin=20 * mm, rightMargin=20 * mm, title=title)
    flow: list = [Paragraph(_esc(title), title_s)]
    if subtitle:
        flow.append(Paragraph(_esc(subtitle), sub_s))
    flow.append(Spacer(1, 4))

    for heading, paragraphs in sections:
        flow.append(Paragraph(_esc(heading), head_s))
        for para in paragraphs:
            text = _esc(para)
            if para.strip().startswith("- "):
                text = "•&nbsp;" + _esc(para.strip()[2:])
            flow.append(Paragraph(text, body_s))
    doc.build(flow)
    return out
