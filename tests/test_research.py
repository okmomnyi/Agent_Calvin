"""Research skill: parse -> gather (authoritative, blocklisted) -> synthesize -> image ->
PDF -> deliver ONCE.

Regression coverage for the real production bugs (see skills/research/skill.py's module
docstring): /research had no route; a format directive ("3-page doc") leaked into the
search query; essay-mill sourcing produced misinformation; and the skill both pushed the
full text via `_notify()` AND returned it as `CommandResult.text` -- a double-send.
"""

from __future__ import annotations

import json
from pathlib import Path

from skills.research.skill import DuckDuckGoSearcher, ResearchResult, ResearchSkill, Source


class _FakeSearcher:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self._results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        self.queries.append(query)
        return self._results


class _FakeFetcher:
    """Every full-page fetch just fails -- gather_sources then falls back to the
    snippet, which is all these orchestration-level tests need."""

    def get(self, url: str, accept: str | None = None):
        return None


class _NoImages:
    def find_image(self, topic: str, download_dir):
        return None


def _skill(fake_llm, results, notified_docs: list, *, synth_json: dict | None = None) -> ResearchSkill:
    fake_llm.post_result = json.dumps(synth_json or {
        "title": "Result", "subtitle": "", "abstract": "An abstract [1].",
        "sections": [{"heading": "Findings", "paragraphs": ["A finding [1]."]}],
        "table": None,
    })
    return ResearchSkill(
        llm=fake_llm,
        searcher=_FakeSearcher(results),
        fetcher=_FakeFetcher(),
        images=_NoImages(),
        notify_document=lambda path, caption="": notified_docs.append((path, caption)) or True,
    )


def _redirect_data_dir(monkeypatch, tmp_path) -> None:
    import skills.research.skill as skill_mod

    real_settings = skill_mod.get_settings()

    class _Settings:
        def __init__(self, real):
            self._real = real
            self.data_dir = tmp_path

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(skill_mod, "get_settings", lambda: _Settings(real_settings))


# ------------------------------------------------------------------ empty/blank input
def test_search_with_an_empty_query_asks_what_to_research(fake_llm):
    notified: list = []
    skill = _skill(fake_llm, [], notified)

    result = skill.search("")

    assert result.ok is False
    assert "research" in result.text.lower()
    assert notified == []


def test_search_with_only_format_directives_and_no_topic_asks_what_to_research(fake_llm):
    notified: list = []
    skill = _skill(fake_llm, [], notified)

    result = skill.search("make a 3-page doc")

    assert result.ok is False
    assert notified == []


# ------------------------------------------------------------------ ALWAYS a PDF, delivered ONCE
def test_search_always_produces_a_delivered_pdf_never_inline_text(fake_llm, monkeypatch, tmp_path):
    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    skill = _skill(fake_llm, [
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
    ], notified)

    result = skill.search("Camaro")

    assert result.ok is True
    assert len(notified) == 1, "the PDF must be delivered exactly once"
    pdf_path, caption = notified[0]
    assert Path(pdf_path).exists()
    assert Path(pdf_path).read_bytes()[:4] == b"%PDF"
    assert result.data["pdf_path"] == pdf_path
    # The text reply is a SHORT confirmation -- never the report's own content.
    assert len(result.text) < 200
    assert "abstract" not in result.text.lower()
    assert "findings" not in result.text.lower()


def test_the_double_send_bug_cannot_recur_notify_is_never_called(fake_llm, monkeypatch, tmp_path):
    """The exact production bug: search() used to call self._notify(...) (a plain text
    push) AND return the same content as CommandResult.text. ResearchSkill has no such
    method any more -- the only delivery path is notify_document, called once."""
    assert not hasattr(ResearchSkill(), "_notify"), \
        "a dead/unused _notify would invite the double-send bug to come back"

    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    skill = _skill(fake_llm, [
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
    ], notified)

    skill.search("Camaro")

    assert len(notified) == 1


def test_notify_false_suppresses_the_document_push_but_still_builds_the_pdf(fake_llm, monkeypatch, tmp_path):
    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    skill = _skill(fake_llm, [
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
    ], notified)

    result = skill.search("Camaro", notify=False)

    assert notified == []
    assert Path(result.data["pdf_path"]).exists()


# ------------------------------------------------------------------ the format-leak bug
def test_a_format_directive_never_leaks_into_the_search_query(fake_llm, monkeypatch, tmp_path):
    """The exact production bug: "Camaro and make a 3page doc" was searched literally."""
    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    searcher = _FakeSearcher([
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
    ])
    fake_llm.post_result = json.dumps({
        "sections": [{"heading": "Findings", "paragraphs": ["A finding [1]."]}], "table": None})
    skill = ResearchSkill(llm=fake_llm, searcher=searcher, fetcher=_FakeFetcher(),
                          images=_NoImages(),
                          notify_document=lambda p, caption="": notified.append(p) or True)

    skill.search("Camaro and make a 3page doc")

    assert len(searcher.queries) == 1
    query = searcher.queries[0]
    assert "page" not in query.lower()
    assert "doc" not in query.lower()
    assert "camaro" in query.lower()


# ------------------------------------------------------------------ essay-mill exclusion
def test_essay_mill_sources_never_reach_the_report(fake_llm, monkeypatch, tmp_path):
    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    searcher = _FakeSearcher([
        {"title": "Camaro essay", "url": "https://www.scribd.com/doc/1/camaro", "snippet": "wrong info"},
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
    ])
    fake_llm.post_result = json.dumps({
        "sections": [{"heading": "Findings", "paragraphs": ["A finding [1]."]}], "table": None})
    skill = ResearchSkill(llm=fake_llm, searcher=searcher, fetcher=_FakeFetcher(),
                          images=_NoImages(),
                          notify_document=lambda p, caption="": notified.append(p) or True)

    result = skill.search("Camaro")

    urls = [s["url"] for s in result.data["sources"]]
    assert not any("scribd.com" in u for u in urls)
    assert any("wikipedia.org" in u for u in urls)


# ------------------------------------------------------------------ degrade path
def test_llm_failure_still_delivers_a_pdf_with_sourced_findings(fake_llm, monkeypatch, tmp_path):
    from core.llm import LLMError

    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    searcher = _FakeSearcher([
        {"title": "Camaro", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro",
         "snippet": "The Camaro was discontinued in 2024."},
    ])

    def _raise(*a, **k):
        raise LLMError("nim down")

    monkeypatch.setattr(fake_llm, "chat_json", _raise, raising=False)
    skill = ResearchSkill(llm=fake_llm, searcher=searcher, fetcher=_FakeFetcher(),
                          images=_NoImages(),
                          notify_document=lambda p, caption="": notified.append(p) or True)

    result = skill.search("Camaro")

    assert result.ok is True
    assert result.data["degraded"] is True
    assert len(notified) == 1
    assert Path(notified[0]).exists()


def test_zero_sources_found_is_reported_as_not_ok_but_still_builds_a_pdf(fake_llm, monkeypatch, tmp_path):
    _redirect_data_dir(monkeypatch, tmp_path)
    notified: list = []
    skill = _skill(fake_llm, [], notified)

    result = skill.search("an utterly obscure topic with nothing findable")

    assert result.ok is False
    assert Path(result.data["pdf_path"]).exists()
    assert len(notified) == 1  # still delivered -- an honest "no sources" report, not nothing


# ------------------------------------------------------------------ internal grounding (research())
# `research()` is a DIFFERENT, narrower thing than `search()` -- other skills
# (interview_prep, study_vault) use it for their OWN quick grounding, never as "the
# research report" delivered to Calvin. It must still never invent a source.
def test_research_method_returns_a_cited_result_for_other_skills_to_use(fake_llm):
    fake_llm.post_result = "Camaros are fast and iconic. [1]"
    skill = ResearchSkill(llm=fake_llm, searcher=_FakeSearcher(
        [{"title": "Camaro", "url": "https://x.test/camaro", "snippet": "..."}]))

    result = skill.research("Camaro")

    assert isinstance(result, ResearchResult)
    assert result.sources == [Source(n=1, title="Camaro", url="https://x.test/camaro", snippet="...")]
    assert "[1]" in result.answer


def test_research_method_with_no_results_says_so_without_inventing(fake_llm):
    skill = ResearchSkill(llm=fake_llm, searcher=_FakeSearcher([]))
    result = skill.research("something obscure")
    assert result.sources == []
    assert "couldn't find" in result.answer.lower()


def test_research_method_degrades_to_raw_snippets_on_llm_failure(fake_llm, monkeypatch):
    from core.llm import LLMError

    def _raise(*a, **k):
        raise LLMError("nim down")

    monkeypatch.setattr(fake_llm, "chat", _raise, raising=False)
    skill = ResearchSkill(llm=fake_llm, searcher=_FakeSearcher(
        [{"title": "Camaro", "url": "https://x.test/camaro", "snippet": "fast car"}]))

    result = skill.research("Camaro")

    assert "fast car" in result.answer


# ------------------------------------------------------------------ DuckDuckGoSearcher (unchanged)
def test_duckduckgo_searcher_is_still_importable_for_interview_prep_and_study_vault():
    """skills/interview_prep.py and skills/study_vault.py both import this directly --
    the package split must not have broken that."""
    assert DuckDuckGoSearcher is not None
