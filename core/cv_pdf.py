"""Render a tailored CV to a clean, professional PDF (Phase 27).

Why this exists: tailored variants were written as Markdown and that `.md` file was what got
ATTACHED to real job applications. An employer would have received a raw markdown file, and
Calvin's emailed copies were unformatted text pasted into the body. A CV is the one document
in this system that a stranger judges him by, so it has to look like a CV.

Two rules shape the layout:

* **Match the master.** His master CV is a classic two-column academic layout -- name, a
  contact line, then sections where the institution/role sits left and the location/dates sit
  right. Variants keep that shape, so a tailored CV still looks like *his* CV.
* **Links must be clickable.** The master's header has Email / LinkedIn / Github / Portfolio
  as real hyperlinks. Flattening those to plain words loses the thing a recruiter actually
  clicks, so the contact details are read out of the master PDF's link annotations and
  re-embedded as live links (ReportLab `<link href>`).

The contact block is EXTRACTED from the master rather than hardcoded: if Calvin changes his
portfolio domain, the variants follow without a code change (§0 -- never invent facts about
him, including his own contact details).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from core.logging_setup import get_logger

log = get_logger("core.cv_pdf")

_INK = colors.HexColor("#1a1a1a")
_MUTED = colors.HexColor("#555555")
_RULE = colors.HexColor("#999999")


@dataclass
class CvContact:
    """The header block. Read from the master CV so it is always Calvin's own detail."""
    name: str = ""
    phone: str = ""
    email: str = ""
    links: dict[str, str] = field(default_factory=dict)   # label -> url

    def is_usable(self) -> bool:
        return bool(self.name)


def extract_contact(master: Path) -> CvContact:
    """Pull name, phone and the header hyperlinks out of the master CV PDF.

    The link URLs come from the PDF's annotation objects, which is where the real targets
    live -- the visible text is just the word "LinkedIn".
    """
    contact = CvContact()
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(master))
        first = reader.pages[0]
        text = first.extract_text() or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            contact.name = lines[0]
        phone = re.search(r"\+?\d[\d\s\-()]{7,}\d", text)
        if phone:
            contact.phone = phone.group(0).strip()

        for annot in first.get("/Annots") or []:
            uri = ((annot.get_object().get("/A") or {}).get("/URI") or "").strip()
            if not uri:
                continue
            low = uri.lower()
            if low.startswith("mailto:"):
                contact.email = uri[7:]
            elif "linkedin." in low:
                contact.links["LinkedIn"] = uri
            elif "github." in low:
                contact.links["GitHub"] = uri
            else:
                contact.links.setdefault("Portfolio", uri)
    except Exception:  # noqa: BLE001 - a CV without a readable header still renders
        log.exception("could not read contact details from %s", master)
    return contact


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle("CvName", parent=base["Title"], fontName="Helvetica-Bold",
                               fontSize=20, leading=24, textColor=_INK, alignment=TA_CENTER,
                               spaceAfter=2),
        "contact": ParagraphStyle("CvContact", parent=base["Normal"], fontName="Helvetica",
                                  fontSize=9, leading=12, textColor=_MUTED,
                                  alignment=TA_CENTER, spaceAfter=8),
        "section": ParagraphStyle("CvSection", parent=base["Heading2"], fontName="Helvetica-Bold",
                                  fontSize=11, leading=13, textColor=_INK, spaceBefore=10,
                                  spaceAfter=2),
        "entry": ParagraphStyle("CvEntry", parent=base["Normal"], fontName="Helvetica-Bold",
                                fontSize=10, leading=13, textColor=_INK),
        "right": ParagraphStyle("CvRight", parent=base["Normal"], fontName="Helvetica-Oblique",
                                fontSize=9, leading=13, textColor=_MUTED, alignment=2),
        "body": ParagraphStyle("CvBody", parent=base["Normal"], fontName="Helvetica",
                               fontSize=9.5, leading=13, textColor=_INK),
        "bullet": ParagraphStyle("CvBullet", parent=base["Normal"], fontName="Helvetica",
                                 fontSize=9.5, leading=13, textColor=_INK,
                                 leftIndent=10, bulletIndent=2, spaceAfter=1),
    }


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _inline(text: str) -> str:
    """Markdown emphasis -> ReportLab markup, and bare URLs -> clickable links."""
    out = _esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<link href="\2" color="#1155cc">\1</link>', out)
    out = re.sub(r"(?<!href=\")(?<!>)(https?://[^\s<)]+)",
                 r'<link href="\1" color="#1155cc">\1</link>', out)
    return out


def _header(contact: CvContact, st: dict) -> list:
    """Name + a single contact line whose links are real, clickable links."""
    flow: list = [Paragraph(_esc(contact.name), st["name"])]
    bits: list[str] = []
    if contact.phone:
        bits.append(_esc(contact.phone))
    if contact.email:
        bits.append(f'<link href="mailto:{contact.email}" color="#1155cc">Email</link>')
    for label, url in contact.links.items():
        bits.append(f'<link href="{_esc(url)}" color="#1155cc">{_esc(label)}</link>')
    if bits:
        flow.append(Paragraph(" &nbsp;|&nbsp; ".join(bits), st["contact"]))
    return flow


def _split_entry(line: str) -> tuple[str, str] | None:
    """'Role — Company, Location, 2024 - Present' -> (left, right) for the two-column look.

    Splits on an em/en dash or a run of spaces, mirroring the master's layout where dates and
    location sit right-aligned against the role on the left.
    """
    m = re.match(r"^(.*?)\s+[—–]\s+(.*)$", line)
    if m and len(m.group(2)) <= 60:
        return m.group(1).strip(), m.group(2).strip()
    # trailing date range ("... 2024 - Present" / "Sept. 2024 – Apr 2028")
    m = re.match(r"^(.*?),?\s+((?:[A-Z][a-z]{2,8}\.?\s*)?\d{4}\s*[-–—]\s*"
                 r"(?:Present|Current|(?:[A-Z][a-z]{2,8}\.?\s*)?\d{4}))\s*$", line)
    if m:
        return m.group(1).strip().rstrip(","), m.group(2).strip()
    return None


def build_cv_pdf(markdown_text: str, out_path: Path, contact: CvContact | None = None,
                 **_: Any) -> Path:
    """Render tailored-CV markdown to a professional PDF. Returns the path written."""
    st = _styles()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"{contact.name if contact else 'CV'} - CV", author=contact.name if contact else "")

    flow: list = []
    if contact and contact.is_usable():
        flow += _header(contact, st)
        flow.append(HRFlowable(width="100%", thickness=0.8, color=_RULE, spaceAfter=4))

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if not title:
                continue
            if level <= 2:                    # a real section: heading + rule
                flow.append(Paragraph(_inline(title.upper()), st["section"]))
                flow.append(HRFlowable(width="100%", thickness=0.6, color=_RULE, spaceAfter=3))
            else:                             # sub-heading inside a section
                flow.append(Paragraph(f"<b>{_inline(title)}</b>", st["body"]))
            continue

        if re.match(r"^[-*+•∗]\s+", stripped):
            text = re.sub(r"^[-*+•∗]\s+", "", stripped)
            flow.append(Paragraph(_inline(text), st["bullet"], bulletText="•"))
            continue

        split = _split_entry(stripped)
        if split:
            left, right = split
            table = Table([[Paragraph(_inline(left), st["entry"]),
                            Paragraph(_inline(right), st["right"])]],
                          colWidths=[118 * mm, 60 * mm])
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            flow.append(KeepTogether(table))
            continue

        flow.append(Paragraph(_inline(stripped), st["body"]))

    if not flow:
        flow.append(Paragraph("(empty CV)", st["body"]))
    doc.build(flow)
    return out_path
