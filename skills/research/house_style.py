"""The one house-style module every research report renders through.

A report on quantum computing and one on Kenyan tax law must come out visually
IDENTICAL -- same cover, same running header/footer, same reference/figure/table style --
so the look lives here, once, and pdf_builder.py only ever calls these helpers. Nothing
about the palette, the byline, or the monogram is ever hardcoded per-report; they all read
from config.yaml's `research` section (see core/config.py), so retuning the look is a
one-line config edit, not a hunt through every report-building call site.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, Image, KeepTogether, Paragraph, Spacer, Table,
                                TableStyle)

from core.config import get_settings

if TYPE_CHECKING:
    from skills.research.gather import FetchedSource
    from skills.research.images import ImageResult

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

_DEFAULT_BYLINE = "Research by Momanyi Kelvin"
_DEFAULT_MONOGRAM = "KM"
_DEFAULT_PALETTE = {
    "ink": "#17352B", "soft": "#4A5A52", "accent": "#B08D57",
    "hairline": "#D3D9D4", "fig_fill": "#EDF1EE",
}


# ------------------------------------------------------------------ config-driven identity
def byline() -> str:
    return get_settings().get("research", "byline", default=_DEFAULT_BYLINE)


def monogram_initials() -> str:
    return get_settings().get("research", "monogram_initials", default=_DEFAULT_MONOGRAM)


def _palette_hex(key: str) -> str:
    return get_settings().get("research", "palette", key, default=_DEFAULT_PALETTE[key])


def ink() -> colors.Color:
    return colors.HexColor(_palette_hex("ink"))


def soft() -> colors.Color:
    return colors.HexColor(_palette_hex("soft"))


def accent() -> colors.Color:
    return colors.HexColor(_palette_hex("accent"))


def hairline() -> colors.Color:
    return colors.HexColor(_palette_hex("hairline"))


def fig_fill() -> colors.Color:
    return colors.HexColor(_palette_hex("fig_fill"))


# ------------------------------------------------------------------ text helpers
def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CITATION_RE = re.compile(r"\[(\d+)\]")


def linkify_citations(text: str) -> str:
    """[3] -> a small brass superscript, so an in-text citation reads as reference
    chrome, not stray bracketed digits sitting in the body copy."""
    accent_hex = _palette_hex("accent")
    return _CITATION_RE.sub(rf'<super><font color="{accent_hex}" size="7">[\1]</font></super>', text)


# ------------------------------------------------------------------ styles
def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "kicker": ParagraphStyle(
            "RKicker", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=11,
            textColor=accent(), leading=13, spaceAfter=6, alignment=TA_LEFT,
        ),
        "cover_title": ParagraphStyle(
            "RCoverTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=27,
            leading=32, textColor=ink(), alignment=TA_LEFT, spaceAfter=6,
        ),
        "cover_subtitle": ParagraphStyle(
            "RCoverSub", parent=base["Normal"], fontName="Times-Italic", fontSize=13,
            leading=18, textColor=soft(), alignment=TA_LEFT, spaceAfter=4,
        ),
        "byline": ParagraphStyle(
            "RByline", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=10.5,
            textColor=accent(), alignment=TA_LEFT, spaceAfter=2,
        ),
        "cover_meta": ParagraphStyle(
            "RCoverMeta", parent=base["Normal"], fontName="Helvetica", fontSize=9,
            textColor=soft(), alignment=TA_LEFT, spaceAfter=2,
        ),
        "section_heading": ParagraphStyle(
            "RSection", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13.5,
            textColor=ink(), spaceBefore=14, spaceAfter=6,
        ),
        "abstract_heading": ParagraphStyle(
            "RAbstractHeading", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=11, textColor=accent(), spaceBefore=4, spaceAfter=4,
        ),
        "abstract": ParagraphStyle(
            "RAbstract", parent=base["Normal"], fontName="Times-Italic", fontSize=10.5,
            leading=15, textColor=ink(), alignment=TA_JUSTIFY, spaceAfter=10,
        ),
        "body": ParagraphStyle(
            "RBody", parent=base["Normal"], fontName="Times-Roman", fontSize=10.5,
            leading=15, textColor=ink(), alignment=TA_JUSTIFY, spaceAfter=7,
        ),
        "caption": ParagraphStyle(
            "RCaption", parent=base["Normal"], fontName="Helvetica", fontSize=8.5,
            leading=11, textColor=soft(), alignment=TA_LEFT, spaceAfter=10,
        ),
        "reference": ParagraphStyle(
            "RReference", parent=base["Normal"], fontName="Times-Roman", fontSize=9.5,
            leading=13, textColor=ink(), spaceAfter=5, leftIndent=14, bulletIndent=0,
        ),
        "toc_title": ParagraphStyle(
            "RTocTitle", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=15,
            textColor=ink(), spaceAfter=10,
        ),
        "toc_entry": ParagraphStyle(
            "RTocEntry", parent=base["Normal"], fontName="Helvetica", fontSize=11,
            textColor=ink(), spaceAfter=6,
        ),
        "table_header": ParagraphStyle(
            "RTableHeader", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=9.5,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "RTableCell", parent=base["Normal"], fontName="Times-Roman", fontSize=9.5,
            textColor=ink(), leading=12,
        ),
        "placeholder": ParagraphStyle(
            "RPlaceholder", parent=base["Normal"], fontName="Helvetica-Oblique", fontSize=9,
            textColor=soft(), alignment=TA_CENTER,
        ),
    }


# ------------------------------------------------------------------ page furniture (header/footer/monogram)
def _draw_monogram(canvas) -> None:
    x, y = MARGIN + 8 * mm, PAGE_H - MARGIN - 6 * mm
    canvas.saveState()
    canvas.setStrokeColor(accent())
    canvas.setLineWidth(1.1)
    canvas.circle(x, y, 8 * mm, stroke=1, fill=0)
    canvas.setFillColor(accent())
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawCentredString(x, y - 3.5, monogram_initials())
    canvas.restoreState()


def _draw_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(hairline())
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, MARGIN - 4 * mm, PAGE_W - MARGIN, MARGIN - 4 * mm)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(accent())
    canvas.drawString(MARGIN, MARGIN - 9 * mm, byline())
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(soft())
    canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _draw_header(canvas, doc, topic: str) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(soft())
    canvas.drawString(MARGIN, PAGE_H - MARGIN + 6 * mm, (topic or "").upper())
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN + 6 * mm, "RESEARCH REPORT")
    canvas.setStrokeColor(hairline())
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, PAGE_H - MARGIN + 3 * mm, PAGE_W - MARGIN, PAGE_H - MARGIN + 3 * mm)
    canvas.restoreState()


def on_first_page(canvas, doc, topic: str = "") -> None:
    """The cover carries the monogram and the signature footer, but no running header --
    the header's job (naming the topic on every OTHER page) is redundant on the one page
    that already states the title in full."""
    _draw_monogram(canvas)
    _draw_footer(canvas, doc)


def on_later_pages(canvas, doc, topic: str) -> None:
    _draw_header(canvas, doc, topic)
    _draw_footer(canvas, doc)


# ------------------------------------------------------------------ cover
def cover_flowables(title: str, subtitle: str, source_count: int,
                    date_str: str | None = None) -> list:
    st = styles()
    date_str = date_str or time.strftime("%d %B %Y")
    flow: list = [
        Spacer(1, 30 * mm),
        Paragraph("RESEARCH REPORT", st["kicker"]),
        HRFlowable(width="35%", thickness=1.4, color=accent(), spaceAfter=10, hAlign="LEFT"),
        Paragraph(esc(title), st["cover_title"]),
    ]
    if subtitle:
        flow.append(Paragraph(esc(subtitle), st["cover_subtitle"]))
    flow.append(Spacer(1, 26 * mm))
    flow.append(Paragraph(esc(byline()), st["byline"]))
    flow.append(Paragraph(
        f"{source_count} authoritative source{'s' if source_count != 1 else ''} consulted",
        st["cover_meta"]))
    flow.append(Paragraph(date_str, st["cover_meta"]))
    return flow


# ------------------------------------------------------------------ contents (5+ page reports only)
def contents_flowables(section_titles: list[str]) -> list:
    st = styles()
    flow: list = [Paragraph("Contents", st["toc_title"])]
    flow.append(Paragraph("Abstract", st["toc_entry"]))
    for i, title in enumerate(section_titles, start=1):
        flow.append(Paragraph(f"{i}. {esc(title)}", st["toc_entry"]))
    flow.append(Paragraph("References", st["toc_entry"]))
    return flow


# ------------------------------------------------------------------ figures
def figure_flowables(image: "ImageResult", number: int) -> list:
    st = styles()
    img = Image(str(image.path), width=90 * mm, height=60 * mm, kind="proportional")
    caption = Paragraph(f"Fig. {number}. {esc(image.caption)}", st["caption"])
    return [KeepTogether([img, Spacer(1, 3), caption])]


def placeholder_figure_flowables(number: int, note: str = "no openly licensed image found") -> list:
    """The typed placeholder -- rendered whenever no image cleared the open-license bar.
    Never a broken <img>, never a substituted unlicensed picture."""
    st = styles()
    box = Table(
        [[Paragraph(f"Figure {number} unavailable — {esc(note)}", st["placeholder"])]],
        colWidths=[90 * mm], rowHeights=[30 * mm],
    )
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fig_fill()),
        ("BOX", (0, 0), (-1, -1), 0.7, hairline()),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return [KeepTogether([box, Spacer(1, 6)])]


# ------------------------------------------------------------------ table
@dataclass
class TableSpec:
    title: str
    headers: list[str]
    rows: list[list[str]]


def table_flowables(spec: TableSpec) -> list:
    st = styles()
    flow: list = []
    if spec.title:
        flow.append(Paragraph(esc(spec.title), st["abstract_heading"]))
    header_row = [Paragraph(esc(h), st["table_header"]) for h in spec.headers]
    body_rows = [[Paragraph(esc(str(c)), st["table_cell"]) for c in row] for row in spec.rows]
    table = Table([header_row, *body_rows], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink()),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, accent()),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, hairline()),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    flow.append(KeepTogether([table, Spacer(1, 8)]))
    return flow


# ------------------------------------------------------------------ references
def reference_flowables(sources: list["FetchedSource"]) -> list:
    """A clean numbered list -- author/title/publisher/date/domain where known, and a
    plain retrieval line always. Never a raw mangled URL sitting bare in the list."""
    st = styles()
    flow: list = [Paragraph("References", st["section_heading"])]
    for s in sources:
        title = esc(s.title) or "(untitled source)"
        domain = esc(s.domain)
        line = f'{s.n}. {title}. <i>{domain}</i>. Retrieved from <link href="{esc(s.url)}">{esc(s.url)}</link>.'
        flow.append(Paragraph(line, st["reference"]))
    return flow


# ------------------------------------------------------------------ optional cover letter
def cover_letter_flowables(to_whom: str, topic: str) -> list:
    st = styles()
    body = (
        f"Dear {esc(to_whom)},\n\n"
        f"Please find attached a research report on {esc(topic)}, prepared to support your "
        "review. I would be glad to discuss any of its findings further.\n\nSincerely,"
    )
    flow: list = [Spacer(1, 20 * mm), Paragraph("Cover Letter", st["section_heading"])]
    for para in body.split("\n\n"):
        flow.append(Paragraph(esc(para).replace("\n", "<br/>"), st["body"]))
    flow.append(Spacer(1, 8 * mm))
    flow.append(Paragraph(esc(byline()), st["byline"]))
    return flow
