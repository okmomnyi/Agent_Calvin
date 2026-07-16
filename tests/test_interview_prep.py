"""Research + interview prep tests: citation grounding, pack generation (no invented facts),
mock state machine, and a real PDF write. All offline (search/LLM injected)."""

from __future__ import annotations

from pathlib import Path

from core.llm import LLMClient, LLMError
from core.memory import Memory
from core.pdf import build_pdf
from skills.research import ResearchSkill, DuckDuckGoSearcher
from skills.interview_prep import InterviewPrepSkill


# ------------------------------------------------------------------ research
class _FakeSearcher:
    def __init__(self, results):
        self._results = results

    def search(self, query, max_results=5):
        return self._results


class _ResearchLLM(LLMClient):
    def __init__(self, answer="Acme builds CI/CD tools [1]. Recently raised funding [2]."):
        self.routes = {"default": "m", "research": "m"}
        self.defaults = {}
        self._answer = answer

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return self._answer


def test_research_synthesizes_with_citations():
    searcher = _FakeSearcher([
        {"title": "Acme Inc", "url": "https://acme.com", "snippet": "CI/CD platform"},
        {"title": "Acme raises $10M", "url": "https://news.com/acme", "snippet": "funding round"},
    ])
    skill = ResearchSkill(llm=_ResearchLLM(), searcher=searcher)
    result = skill.research("what does Acme do")
    assert len(result.sources) == 2
    assert result.sources[0].url == "https://acme.com"
    assert "[1]" in result.cited_text()
    assert "Sources:" in result.cited_text()


def test_research_no_results_does_not_fabricate():
    skill = ResearchSkill(llm=_ResearchLLM(), searcher=_FakeSearcher([]))
    result = skill.research("obscure query")
    assert result.sources == []
    assert "couldn't find" in result.answer.lower()


def test_ddg_parser_extracts_results():
    html = """
    <div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.com">
    Acme Inc</a><a class="result__snippet">CI/CD platform for teams</a></div>
    <div class="result"><a class="result__a" href="https://news.com/acme">Acme raises funds</a></div>
    """

    class _Resp:
        text = html
        status_code = 200

    class _Fetcher:
        def get(self, url, accept=None):
            return _Resp()

    searcher = DuckDuckGoSearcher(fetcher=_Fetcher())
    results = searcher.search("acme")
    assert results[0]["url"] == "https://acme.com"
    assert "CI/CD" in results[0]["snippet"]
    assert results[1]["title"] == "Acme raises funds"


# ------------------------------------------------------------------ prep pack
class _PrepLLM(LLMClient):
    def __init__(self, pack):
        self.routes = {"default": "m", "write": "m", "research": "m"}
        self.defaults = {}
        self._pack = pack

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "Acme builds CI/CD tools."

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return self._pack


def _pack_payload():
    return {
        "company_summary": "Acme builds developer CI/CD tooling.",
        "questions": [{"q": f"Question {i}?", "a": f"Answer {i}"} for i in range(1, 16)],
        "ask_them": ["What does success look like in 90 days?", "How is the team structured?"],
        "checklist": ["Test your webcam", "Have examples ready"],
    }


def _prep_skill(mem):
    research = ResearchSkill(llm=_ResearchLLM(), searcher=_FakeSearcher(
        [{"title": "Acme", "url": "https://acme.com", "snippet": "CI/CD"}]))
    return InterviewPrepSkill(llm=_PrepLLM(_pack_payload()), memory=mem, research=research)


def test_generate_pack_structure(mem):
    skill = _prep_skill(mem)
    pack = skill.generate_pack("Acme", role="DevOps")
    assert len(pack["questions"]) == 15
    assert pack["company"] == "Acme"
    assert pack["sources"]  # research sources carried through


def test_prep_writes_pdf(mem, tmp_path, monkeypatch):
    skill = _prep_skill(mem)
    # redirect data_dir to tmp so the PDF lands in a throwaway place
    import skills.interview_prep as ip

    real_settings = ip.get_settings()

    class _S:
        def __init__(self, r): self._r = r; self.data_dir = tmp_path
        def __getattr__(self, n): return getattr(self._r, n)

    monkeypatch.setattr(ip, "get_settings", lambda: _S(real_settings))
    result = skill.prep(company="Acme", notify=False)
    pdf = Path(result.data["pdf"])
    assert pdf.exists()
    assert pdf.stat().st_size > 1000  # a real PDF was written
    assert result.data["questions"] == 15


# ------------------------------------------------------------------ mock interview
class _MockLLM(LLMClient):
    def __init__(self):
        self.routes = {"default": "m", "write": "m", "research": "m"}
        self.defaults = {}

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "Solid structure; add a concrete metric next time."

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return {"company_summary": "x", "questions": [{"q": "Q1?", "a": "a"}, {"q": "Q2?", "a": "a"}],
                "ask_them": [], "checklist": []}


def test_mock_state_machine_advances_and_finishes(mem):
    research = ResearchSkill(llm=_ResearchLLM(), searcher=_FakeSearcher([{"title": "t", "url": "u", "snippet": "s"}]))
    skill = InterviewPrepSkill(llm=_MockLLM(), memory=mem, research=research)

    start = skill.mock(company="Acme")
    assert start.data["index"] == 0
    assert "Q1" in start.text

    a1 = skill.mock_answer(answer="I did X and measured Y.")
    assert a1.data["done"] is False
    assert "Q2" in a1.text

    a2 = skill.mock_answer(answer="Another answer.")
    assert a2.data["done"] is True
    # session cleared after finishing
    assert not mem.kv_get("interview_prep.mock")


def test_mock_answer_without_session_is_graceful(mem):
    research = ResearchSkill(llm=_ResearchLLM(), searcher=_FakeSearcher([]))
    skill = InterviewPrepSkill(llm=_MockLLM(), memory=mem, research=research)
    result = skill.mock_answer(answer="hi")
    assert result.ok is False


# ------------------------------------------------------------------ pdf helper
def test_build_pdf_creates_file(tmp_path):
    out = build_pdf(tmp_path / "t.pdf", "Test Doc",
                    [("Section A", ["Para one", "- bullet two"])], subtitle="sub")
    assert out.exists() and out.stat().st_size > 800
