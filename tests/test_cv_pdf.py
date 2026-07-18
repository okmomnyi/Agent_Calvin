"""Tailored CVs render as real PDFs (Phase 27).

The bug this closes: variants were saved as Markdown and that .md file was ATTACHED to real
job applications -- an employer would have received a raw markdown file. Calvin's own copies
arrived as unformatted text pasted into an email body.

Two properties matter:
  * the output is a genuine PDF, and it is what the application attaches;
  * the header links are CLICKABLE annotations, not words that merely say "LinkedIn".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cv_pdf import CvContact, build_cv_pdf

MD = """# Education
Meru University of Science and Technology — Meru, KE
* BSc Computer Science, Sept 2024 - Apr 2028

# Experience
Independent Cybersecurity & Cloud Projects — 2024 - Present
* Deployed applications using modern cloud platforms
* Built and tested web applications

# Skills
## Infrastructure
* Docker / containerisation
* PostgreSQL
"""

CONTACT = CvContact(
    name="Kelvin Momanyi", phone="+254 707-619-453", email="okmomanyi@gmail.com",
    links={"LinkedIn": "https://linkedin.com/in/kelvin-momanyi",
           "GitHub": "https://github.com/okmomnyi",
           "Portfolio": "https://kelvinmomanyi.codes"})


def _links(pdf: Path) -> list[str]:
    from pypdf import PdfReader

    out = []
    for page in PdfReader(str(pdf)).pages:
        for annot in page.get("/Annots") or []:
            uri = ((annot.get_object().get("/A") or {}).get("/URI") or "")
            if uri:
                out.append(uri)
    return out


def test_output_is_a_real_pdf(tmp_path):
    out = build_cv_pdf(MD, tmp_path / "cv.pdf", CONTACT)
    assert out.exists() and out.suffix == ".pdf"
    assert out.read_bytes()[:5] == b"%PDF-", "not a PDF -- an employer would get a text file"


def test_header_links_are_clickable_not_just_words(tmp_path):
    """The master CV's Email/LinkedIn/Github/Portfolio are hyperlinks. Flattening them to
    plain text loses the thing a recruiter actually clicks."""
    out = build_cv_pdf(MD, tmp_path / "cv.pdf", CONTACT)
    links = _links(out)
    assert "mailto:okmomanyi@gmail.com" in links
    assert any("linkedin.com/in/kelvin-momanyi" in u for u in links)
    assert any("github.com/okmomnyi" in u for u in links)
    assert any("kelvinmomanyi.codes" in u for u in links)


def test_content_survives_rendering(tmp_path):
    from pypdf import PdfReader

    out = build_cv_pdf(MD, tmp_path / "cv.pdf", CONTACT)
    text = " ".join((p.extract_text() or "") for p in PdfReader(str(out)).pages)
    assert "Kelvin Momanyi" in text
    assert "Docker" in text and "PostgreSQL" in text
    assert "EDUCATION" in text.upper() and "EXPERIENCE" in text.upper()


def test_renders_without_contact_details(tmp_path):
    """A CV with no readable master header must still produce a usable document."""
    out = build_cv_pdf(MD, tmp_path / "cv.pdf", None)
    assert out.read_bytes()[:5] == b"%PDF-"


def test_markdown_links_in_the_body_are_clickable(tmp_path):
    md = "# Projects\n* [AgentOS](https://github.com/okmomnyi/Agent_Calvin) - agentic system\n"
    out = build_cv_pdf(md, tmp_path / "cv.pdf", CONTACT)
    assert any("Agent_Calvin" in u for u in _links(out))


def test_the_tailored_variant_handed_to_applications_is_a_pdf(mem, tmp_path, monkeypatch):
    """The whole point: _save_variant returns what gets ATTACHED, so it must be the PDF."""
    import skills.cv_tailor as ct

    real = ct.get_settings()

    class _S:
        def __init__(self): self.data_dir = tmp_path
        def __getattr__(self, n): return getattr(real, n)

    monkeypatch.setattr(ct, "get_settings", lambda: _S())
    skill = ct.CvTailorSkill(memory=mem, llm=None, clock=lambda: 1_000.0)
    out = skill._save_variant("42", "Acme", MD)
    assert out.suffix == ".pdf", "applications would attach a .md to a real employer"
    assert out.read_bytes()[:5] == b"%PDF-"
    # the markdown source is kept alongside for editing
    assert out.with_suffix(".md").exists()
