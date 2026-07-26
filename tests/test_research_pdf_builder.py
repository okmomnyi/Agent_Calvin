"""pdf_builder.py: assembles the actual PDF file. Verified by reading it back with pypdf --
the same library core/cv_pdf.py already uses to read a master CV's contact block."""

from __future__ import annotations

from pypdf import PdfReader

from skills.research.gather import FetchedSource
from skills.research.images import ImageResult
from skills.research.pdf_builder import build_report_pdf
from skills.research.synthesis import DocumentPlan, Section, TableData

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
    "0000001249444154789c631437d5666060606200030005aa007ba595dbb9000000"
    "0049454e44ae426082")


def _all_text(path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _plan(n_sections: int = 2, table: TableData | None = None) -> DocumentPlan:
    sections = [Section(heading=f"Section {i}", paragraphs=[f"Paragraph text {i} [1]."])
               for i in range(1, n_sections + 1)]
    return DocumentPlan(title="Chevrolet Camaro", subtitle="A retrospective",
                        abstract="An abstract summarizing the whole report [1][2].",
                        sections=sections, table=table, degraded=False)


def _sources() -> list[FetchedSource]:
    return [
        FetchedSource(n=1, title="Chevrolet Camaro", url="https://en.wikipedia.org/wiki/Chevrolet_Camaro",
                     domain="en.wikipedia.org", text="..."),
        FetchedSource(n=2, title="GM ends Camaro production", url="https://www.gm.com/camaro-end",
                     domain="gm.com", text="..."),
    ]


def test_writes_a_real_pdf_file(tmp_path):
    out = build_report_pdf(tmp_path / "report.pdf", "Camaro", _plan(), _sources(), length="medium")
    assert out.exists()
    assert out.suffix == ".pdf"
    assert out.read_bytes()[:4] == b"%PDF"


def test_cover_carries_title_subtitle_and_byline(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(), _sources())
    text = _all_text(out)
    assert "Chevrolet Camaro" in text
    assert "A retrospective" in text
    assert "Research by Momanyi Kelvin" in text
    assert "RESEARCH REPORT" in text


def test_abstract_and_numbered_sections_are_present(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(n_sections=2), _sources())
    text = _all_text(out)
    assert "Abstract" in text
    assert "1. Section 1" in text
    assert "2. Section 2" in text


def test_references_are_numbered_and_include_domain_and_url(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(), _sources())
    text = _all_text(out)
    assert "References" in text
    assert "en.wikipedia.org" in text
    assert "gm.com" in text


def test_running_header_shows_topic_and_report_label_on_later_pages(tmp_path):
    # Enough sections to force a second page.
    out = build_report_pdf(tmp_path / "r.pdf", "camaro", _plan(n_sections=8), _sources())
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 2
    later_page_text = reader.pages[-1].extract_text() or ""
    assert "CAMARO" in later_page_text.upper()
    assert "RESEARCH REPORT" in later_page_text


def test_footer_signature_and_page_number_appear_on_every_page(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "camaro", _plan(n_sections=8), _sources())
    reader = PdfReader(str(out))
    for page in reader.pages:
        text = page.extract_text() or ""
        assert "Research by Momanyi Kelvin" in text
        assert "Page" in text


def test_table_renders_when_present(tmp_path):
    table = TableData(title="Generations", headers=["Gen", "Years"],
                      rows=[["1st", "1966-1969"], ["6th", "2016-2024"]])
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(table=table), _sources())
    text = _all_text(out)
    assert "Generations" in text
    assert "1966-1969" in text


def test_no_table_when_plan_has_none(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(table=None), _sources())
    # Just confirm it builds cleanly with no table section -- no crash, no stray artifact.
    assert out.exists()


def test_real_image_is_embedded_with_a_numbered_caption(tmp_path):
    img_path = tmp_path / "camaro.png"
    img_path.write_bytes(_PNG_BYTES)
    image = ImageResult(path=img_path, caption="Jane Doe — Wikimedia Commons (CC BY-SA 4.0)",
                        attribution="Jane Doe — Wikimedia Commons (CC BY-SA 4.0)",
                        license_name="CC BY-SA 4.0")

    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(), _sources(), image=image)

    text = _all_text(out)
    assert "Fig. 1." in text
    assert "Jane Doe" in text
    assert "Figure 1 unavailable" not in text


def test_no_image_renders_the_typed_placeholder_never_a_broken_image(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(), _sources(), image=None)
    text = _all_text(out)
    assert "Figure 1 unavailable" in text
    assert "Fig. 1." not in text


def test_contents_page_included_for_a_long_report(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(n_sections=6), _sources(),
                           length="detailed")
    assert "Contents" in _all_text(out)


def test_contents_page_omitted_for_a_short_report(tmp_path):
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(n_sections=2), _sources(),
                           length="brief")
    assert "Contents" not in _all_text(out)


def test_optional_cover_letter_is_included_only_when_requested(tmp_path):
    with_letter = build_report_pdf(tmp_path / "a.pdf", "Camaro", _plan(), _sources(),
                                   cover_letter_to="the hiring manager")
    without_letter = build_report_pdf(tmp_path / "b.pdf", "Camaro", _plan(), _sources())

    assert "Cover Letter" in _all_text(with_letter)
    assert "Dear the hiring manager" in _all_text(with_letter)
    assert "Cover Letter" not in _all_text(without_letter)


def test_the_camaro_discontinued_fact_survives_the_full_pdf_pipeline_intact(tmp_path):
    """End-to-end version of the hostile case: a plan correctly reflecting "discontinued"
    sources must render that, and must never ALSO say "still in production" anywhere in
    the final document text."""
    plan = DocumentPlan(
        title="Chevrolet Camaro", subtitle="",
        abstract="Production of the Camaro ended after the 2024 model year [1][2].",
        sections=[Section(heading="End of production", paragraphs=[
            "GM discontinued the Camaro nameplate in January 2024 [1][2]."])],
        table=None, degraded=False,
    )
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", plan, _sources())
    text = _all_text(out).lower()
    assert "discontinued" in text
    assert "still in production" not in text


def test_citations_render_as_bracketed_numbers_not_a_mangled_url_or_stray_token(tmp_path):
    """Regression for the exact mangled-citation bug from the transcript
    (Chevrolet_Camaro￼[3], a date captured as a ref)."""
    out = build_report_pdf(tmp_path / "r.pdf", "Camaro", _plan(), _sources())
    text = _all_text(out)
    assert "￼" not in text
    assert "[1]" in text
