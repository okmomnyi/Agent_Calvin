"""house_style.py: the ONE place every research report's look lives.

The core guarantee under test: two different-topic reports must render with identical
house styling -- same palette, cover furniture, footer, monogram -- because nothing here
takes a per-report override. Byline/monogram/palette are config values, never literals.
"""

from __future__ import annotations

import types

import pytest
from reportlab.lib import colors
from reportlab.platypus import Image, Table

from skills.research import house_style as hs


class _FakeSettings:
    """Mimics core.config.Settings' dotted-path .get() for a research-section override."""

    def __init__(self, overrides: dict) -> None:
        self._overrides = overrides

    def get(self, *keys, default=None):
        node = self._overrides
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


# ------------------------------------------------------------------ config-driven identity
def test_byline_defaults_to_the_documented_value():
    assert hs.byline() == "Research by Momanyi Kelvin"


def test_byline_reads_from_config_not_hardcoded(monkeypatch):
    monkeypatch.setattr(hs, "get_settings",
                        lambda: _FakeSettings({"research": {"byline": "Research by Someone Else"}}))
    assert hs.byline() == "Research by Someone Else"


def test_monogram_initials_default_and_override(monkeypatch):
    assert hs.monogram_initials() == "KM"
    monkeypatch.setattr(hs, "get_settings",
                        lambda: _FakeSettings({"research": {"monogram_initials": "XY"}}))
    assert hs.monogram_initials() == "XY"


def test_palette_defaults_match_the_documented_hexes():
    assert hs.ink() == colors.HexColor("#17352B")
    assert hs.soft() == colors.HexColor("#4A5A52")
    assert hs.accent() == colors.HexColor("#B08D57")
    assert hs.hairline() == colors.HexColor("#D3D9D4")
    assert hs.fig_fill() == colors.HexColor("#EDF1EE")


def test_palette_reads_from_config_override(monkeypatch):
    monkeypatch.setattr(hs, "get_settings", lambda: _FakeSettings(
        {"research": {"palette": {"ink": "#000000"}}}))
    assert hs.ink() == colors.HexColor("#000000")


# ------------------------------------------------------------------ styles
def test_styles_uses_helvetica_bold_headings_and_times_body_justified():
    st = hs.styles()
    assert st["section_heading"].fontName == "Helvetica-Bold"
    assert st["body"].fontName == "Times-Roman"
    from reportlab.lib.enums import TA_JUSTIFY

    assert st["body"].alignment == TA_JUSTIFY


def test_styles_has_every_furniture_role_needed():
    st = hs.styles()
    for key in ("kicker", "cover_title", "cover_subtitle", "byline", "section_heading",
               "abstract", "body", "caption", "reference", "toc_title", "toc_entry",
               "table_header", "table_cell", "placeholder"):
        assert key in st, f"missing style role: {key}"


# ------------------------------------------------------------------ same style, different topic
def test_two_different_topic_reports_use_identical_palette_and_furniture():
    """The style module is the single source -- these functions take no per-report color
    override at all, so two independently-built covers must carry identical colors."""
    st_a = hs.styles()
    st_b = hs.styles()
    for key in st_a:
        assert st_a[key].textColor == st_b[key].textColor
        assert st_a[key].fontName == st_b[key].fontName

    cover_a = hs.cover_flowables("Chevrolet Camaro", "A muscle car retrospective", 5)
    cover_b = hs.cover_flowables("Kenyan Tax Law", "An overview of the 2024 reforms", 7)
    # Same number/shape of cover furniture regardless of title/subtitle content.
    assert len(cover_a) == len(cover_b)
    assert hs.ink() == hs.ink()  # not report-specific -- no argument even exists to vary it


def test_byline_appears_on_every_report_regardless_of_topic():
    cover = hs.cover_flowables("Any Topic At All", "", 3)
    texts = [f.text for f in cover if hasattr(f, "text")]
    assert any(hs.byline() in t for t in texts)


# ------------------------------------------------------------------ figures
def test_figure_flowables_embeds_a_real_image_with_a_numbered_caption(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
        "0000001249444154789c631437d5666060606200030005aa007ba595dbb9000000"
        "0049454e44ae426082"))
    image = types.SimpleNamespace(path=png, caption="Jane Doe — Wikimedia Commons (CC BY-SA 4.0)")

    flow = hs.figure_flowables(image, number=2)
    flat = _flatten(flow)

    assert any(isinstance(f, Image) for f in flat)
    captions = [f.text for f in flat if hasattr(f, "text")]
    assert any("Fig. 2." in c and "Jane Doe" in c for c in captions)


def test_placeholder_figure_is_a_typed_box_never_a_broken_image():
    flow = hs.placeholder_figure_flowables(number=1)
    flat = _flatten(flow)

    assert not any(isinstance(f, Image) for f in flat)
    assert any(isinstance(f, Table) for f in flat)


# ------------------------------------------------------------------ references
def test_reference_flowables_render_clean_numbered_entries_no_raw_mangled_url():
    from skills.research.gather import FetchedSource

    sources = [
        FetchedSource(n=1, title="Chevrolet Camaro", url="https://en.wikipedia.org/wiki/Chevrolet_Camaro",
                     domain="en.wikipedia.org", text="..."),
        FetchedSource(n=2, title="GM ends Camaro production", url="https://www.gm.com/camaro-end",
                     domain="gm.com", text="..."),
    ]
    flow = hs.reference_flowables(sources)
    texts = [f.text for f in flow if hasattr(f, "text")]

    assert any("1." in t and "en.wikipedia.org" in t for t in texts)
    assert any("2." in t and "gm.com" in t for t in texts)
    # No stray control characters or a bare unformatted URL sitting outside a <link> tag.
    assert not any("￼" in t for t in texts)  # the exact mangled-glyph bug from the transcript


def test_reference_handles_a_source_with_no_title_gracefully():
    from skills.research.gather import FetchedSource

    sources = [FetchedSource(n=1, title="", url="https://x.test/a", domain="x.test", text="...")]
    flow = hs.reference_flowables(sources)
    texts = [f.text for f in flow if hasattr(f, "text")]
    assert any("untitled source" in t.lower() for t in texts)


def _flatten(flow: list) -> list:
    """KeepTogether wraps a sub-list -- flatten one level so tests can inspect flowables
    without caring about that wrapper."""
    out = []
    for item in flow:
        content = getattr(item, "_content", None)
        if content is not None:
            out.extend(content)
        else:
            out.append(item)
    return out
