"""Scoring tests: structured output, unpaid auto-skip, heuristic fallback on LLM failure,
deterministic seniority penalty, and hard pre-LLM filters (unpaid phrasing the model missed,
language requirements outside Calvin's known set)."""

from __future__ import annotations

from core.llm import LLMClient, LLMError
from skills.job_hunter.scoring import hard_reject, score_job, seniority_multiplier
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


# ================================================================= seniority multiplier
def test_seniority_multiplier_by_title():
    assert seniority_multiplier("Director of Cloud Operations") == 0.1
    assert seniority_multiplier("Senior Manager, DevOps") == 0.2
    assert seniority_multiplier("Principal DevOps Engineer") == 0.2
    assert seniority_multiplier("Senior Software Engineer, Infrastructure") == 0.5
    assert seniority_multiplier("Graduate DevOps Engineer") == 1.2
    assert seniority_multiplier("Cloud Support Engineer") == 1.0  # no signal -> unchanged


def test_senior_director_matches_the_stronger_director_penalty_not_senior():
    assert seniority_multiplier("Senior Director of Engineering") == 0.1


def test_score_job_applies_seniority_penalty_the_model_named_but_never_subtracted():
    """Telegram log: the model's own rationale said 'senior level fits associate target' and
    still returned 80. The penalty must now be applied in code regardless of what the model
    does with it in the score field."""
    llm = _ScoringLLM({"score": 80, "category": "cloud_devops",
                       "reason": "senior level fits associate cloud engineer target",
                       "unpaid": False})
    s = score_job(llm, _job("Senior Staff Software Engineer, Core Automation"))
    assert s.score == round(80 * 0.2)  # "staff" -> 0.2, matched ahead of "senior"


def test_unpaid_score_is_not_further_penalized_by_seniority():
    llm = _ScoringLLM({"score": 70, "category": "internship", "reason": "unpaid", "unpaid": True})
    s = score_job(llm, _job("Senior Unpaid Intern", "internship"))
    assert s.score == 0  # unpaid short-circuits before the multiplier, stays exactly 0


# ================================================================= hard filters (pre-LLM)
def test_equity_only_is_hard_rejected_before_the_llm_is_even_asked():
    llm = _ScoringLLM(raise_error=True)  # would blow up if score_job tried to call it
    job = _job("Cloud Infrastructure Engineer (Part-Time, Equity-Only)")
    s = score_job(llm, job)
    assert s.score == 0
    assert "hard filter" in s.reason


def test_hard_reject_flags_equity_only():
    job = _job("Cloud Infrastructure Engineer (Part-Time, Equity-Only)")
    assert hard_reject(job) is not None


def test_hard_reject_flags_a_language_calvin_does_not_speak():
    job = _job("Project Chiron — Spanish (Latin America) QC Specialist")
    reason = hard_reject(job)
    assert reason is not None and "spanish" in reason.lower()


def test_hard_reject_does_not_flag_english_or_swahili():
    job = _job("Bilingual English/Swahili Transcription QC")
    assert hard_reject(job) is None


def test_hard_reject_passes_a_clean_job_through():
    assert hard_reject(_job()) is None


def test_language_hard_reject_is_short_circuited_before_the_llm():
    llm = _ScoringLLM(raise_error=True)
    job = _job("Project Chiron — French (Canada) QC Specialist")
    s = score_job(llm, job)
    assert s.score == 0
    assert "hard filter" in s.reason
