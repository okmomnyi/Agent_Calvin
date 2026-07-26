"""synthesis.py: structured document synthesis, grounded in fetched sources only.

The hostile case this exists to catch: the Camaro production error from the real
transcript -- sources saying "discontinued" must never come out the other end saying
"still in production". These tests can't prove the REAL model is honest (fake_llm returns
whatever JSON the test scripts), but they prove this module's own plumbing never injects,
overrides, or survives-a-crash into that specific wrong claim.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.llm import LLMError
from skills.research.gather import FetchedSource
from skills.research.synthesis import synthesize


def _source(n: int, title: str, url: str, text: str) -> FetchedSource:
    return FetchedSource(n=n, title=title, url=url, domain=url.split("/")[2], text=text,
                         snippet=text[:100])


def test_no_sources_degrades_honestly_without_calling_the_llm(fake_llm):
    plan = synthesize(fake_llm, "an obscure topic", [], length="medium")

    assert plan.degraded is True
    assert plan.sections == []
    assert "no verifiable" in plan.abstract.lower()
    assert fake_llm.calls == []


def test_successful_synthesis_parses_sections_and_table(fake_llm):
    fake_llm.post_result = json.dumps({
        "title": "Chevrolet Camaro", "subtitle": "A retrospective",
        "abstract": "The Camaro was a long-running muscle car [1][2].",
        "sections": [
            {"heading": "Origins", "paragraphs": ["Introduced in 1966 [1]."]},
            {"heading": "End of production", "paragraphs": ["Production ended in 2024 [2]."]},
        ],
        "table": {"title": "Generations", "headers": ["Gen", "Years"],
                  "rows": [["1st", "1966-1969"], ["6th", "2016-2024"]]},
    })
    sources = [_source(1, "Camaro history", "https://en.wikipedia.org/wiki/Camaro", "..."),
              _source(2, "GM ends Camaro", "https://www.gm.com/camaro-end", "...")]

    plan = synthesize(fake_llm, "Camaro", sources, length="medium")

    assert plan.degraded is False
    assert plan.title == "Chevrolet Camaro"
    assert [s.heading for s in plan.sections] == ["Origins", "End of production"]
    assert plan.table is not None
    assert plan.table.headers == ["Gen", "Years"]
    assert len(plan.table.rows) == 2


def test_table_is_none_when_the_model_omits_it(fake_llm):
    fake_llm.post_result = json.dumps({
        "title": "T", "subtitle": "", "abstract": "A.",
        "sections": [{"heading": "H", "paragraphs": ["P [1]."]}],
        "table": None,
    })
    plan = synthesize(fake_llm, "topic", [_source(1, "T", "https://x.test/a", "...")])
    assert plan.table is None


def test_table_with_no_rows_is_treated_as_no_table(fake_llm):
    fake_llm.post_result = json.dumps({
        "title": "T", "subtitle": "", "abstract": "A.",
        "sections": [{"heading": "H", "paragraphs": ["P [1]."]}],
        "table": {"title": "Empty", "headers": ["A"], "rows": []},
    })
    plan = synthesize(fake_llm, "topic", [_source(1, "T", "https://x.test/a", "...")])
    assert plan.table is None


def test_llm_error_degrades_to_a_sourced_fact_list_never_silent(fake_llm, monkeypatch):
    def _raise(*a, **k):
        raise LLMError("nim down")

    monkeypatch.setattr(fake_llm, "chat_json", _raise, raising=False)
    sources = [_source(1, "Camaro history", "https://en.wikipedia.org/wiki/Camaro",
                       "The Camaro was discontinued in January 2024.")]

    plan = synthesize(fake_llm, "Camaro", sources)

    assert plan.degraded is True
    assert plan.sections[0].heading == "Findings"
    assert "[1]" in plan.sections[0].paragraphs[0]
    assert "unavailable" in plan.abstract.lower()


def test_a_response_with_no_usable_sections_also_degrades(fake_llm):
    fake_llm.post_result = json.dumps({"title": "T", "sections": []})
    sources = [_source(1, "Camaro history", "https://en.wikipedia.org/wiki/Camaro",
                       "Discontinued 2024.")]

    plan = synthesize(fake_llm, "Camaro", sources)

    assert plan.degraded is True
    assert plan.sections[0].heading == "Findings"


# ------------------------------------------------------------------ the Camaro hostile case
def test_sources_saying_discontinued_never_come_out_saying_still_in_production(fake_llm):
    """The exact production bug. Sources are unambiguous: discontinued 2024. A correctly
    scripted synthesis reflecting that must round-trip intact -- and, separately, the
    module's own source code must never hardcode the wrong claim anywhere (see the scan
    test below)."""
    fake_llm.post_result = json.dumps({
        "title": "Chevrolet Camaro", "subtitle": "",
        "abstract": "The Camaro's sixth generation ended production in January 2024 [1][2].",
        "sections": [
            {"heading": "End of production", "paragraphs": [
                "GM ceased Camaro production at its Lansing Grand River plant in January "
                "2024, ending the nameplate after six generations [1][2]."]},
        ],
        "table": None,
    })
    sources = [
        _source(1, "Chevrolet Camaro", "https://en.wikipedia.org/wiki/Chevrolet_Camaro",
               "The sixth-generation Camaro was discontinued after the 2024 model year."),
        _source(2, "GM ends Camaro production", "https://www.gm.com/camaro-end",
               "General Motors built the final Camaro in January 2024."),
    ]

    plan = synthesize(fake_llm, "Camaro", sources, length="brief")

    rendered = plan.abstract + " ".join(p for s in plan.sections for p in s.paragraphs)
    assert "still in production" not in rendered.lower()
    assert "discontinued" in rendered.lower() or "ended" in rendered.lower() or "ceased" in rendered.lower()


def test_synthesis_module_never_hardcodes_the_wrong_claim():
    """A static guardrail: the exact wrong sentence from the real transcript must never
    appear literally anywhere in this module's own source, now or in a future edit."""
    source = Path(__file__).parents[1].joinpath(
        "skills", "research", "synthesis.py").read_text(encoding="utf-8")
    assert "still in production" not in source.lower()


def test_length_affects_the_section_budget_in_the_prompt(fake_llm):
    fake_llm.post_result = json.dumps({"sections": [{"heading": "H", "paragraphs": ["p [1]"]}]})
    sources = [_source(1, "T", "https://x.test/a", "...")]

    synthesize(fake_llm, "topic", sources, length="brief")
    brief_prompt = fake_llm.calls[-1]["messages"][1]["content"]

    synthesize(fake_llm, "topic", sources, length="detailed")
    detailed_prompt = fake_llm.calls[-1]["messages"][1]["content"]

    assert brief_prompt != detailed_prompt


def test_never_copies_source_text_verbatim_beyond_the_degrade_paths_own_snippet(fake_llm):
    """The degrade path's own fact list legitimately quotes the snippet directly (it says
    so honestly via the section heading "Findings") -- but a SUCCESSFUL synthesis's system
    prompt must instruct paraphrasing, not verbatim copying."""
    fake_llm.post_result = json.dumps({"sections": [{"heading": "H", "paragraphs": ["p [1]"]}]})
    synthesize(fake_llm, "topic", [_source(1, "T", "https://x.test/a", "...")])
    system_prompt = fake_llm.calls[-1]["messages"][1]["content"]
    assert "paraphrase" in system_prompt.lower() or "own words" in system_prompt.lower()
    assert "never copy" in system_prompt.lower() or "never invent" in system_prompt.lower()
