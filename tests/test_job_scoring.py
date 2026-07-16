"""Scoring tests: structured output, unpaid auto-skip, heuristic fallback on LLM failure."""

from __future__ import annotations

from core.llm import LLMClient, LLMError
from skills.job_hunter.scoring import score_job
from skills.job_hunter.sources.base import RawJob


class _ScoringLLM(LLMClient):
    def __init__(self, payload=None, raise_error=False):
        self.routes = {"default": "m", "classify": "m"}
        self.defaults = {}
        self._payload = payload
        self._raise = raise_error

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        if self._raise:
            raise LLMError("down")
        return self._payload


def _job(title="DevOps Engineer", hint="cloud_devops"):
    return RawJob(source="s", external_id="1", title=title, description="k8s and terraform",
                  category_hint=hint)


def test_score_job_parses_structured_result():
    llm = _ScoringLLM({"score": 82, "category": "cloud_devops", "reason": "great fit", "unpaid": False})
    s = score_job(llm, _job())
    assert s.score == 82
    assert s.category == "cloud_devops"
    assert s.reason == "great fit"


def test_unpaid_forces_zero_score():
    llm = _ScoringLLM({"score": 70, "category": "internship", "reason": "unpaid", "unpaid": True})
    s = score_job(llm, _job("Unpaid Intern", "internship"))
    assert s.score == 0
    assert s.unpaid is True


def test_score_clamped_and_category_validated():
    llm = _ScoringLLM({"score": 999, "category": "bogus", "reason": "x", "unpaid": False})
    s = score_job(llm, _job())
    assert s.score == 100                 # clamped
    assert s.category == "cloud_devops"   # invalid category falls back to the hint


def test_heuristic_fallback_on_llm_error():
    llm = _ScoringLLM(raise_error=True)
    s = score_job(llm, _job())
    assert s.category == "cloud_devops"
    assert 0 < s.score < 100  # conservative heuristic, not a hard fail
