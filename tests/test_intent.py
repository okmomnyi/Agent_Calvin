"""Intent router tests: keyword matching, arg extraction, and LLM fallback (mocked)."""

from __future__ import annotations

import pytest

from core.intent import IntentRouter
from core.llm import LLMClient


@pytest.fixture
def router(fake_llm):
    return IntentRouter(llm=fake_llm)


@pytest.mark.parametrize(
    "text,expected_intent,expected_skill",
    [
        ("any new jobs?", "find_jobs", "job_hunter"),
        ("check my email", "check_email", "email_agent"),
        ("summarize my inbox", "summarize_inbox", "email_agent"),
        ("any free events?", "find_events", "event_scout"),
        ("any CTFs coming up?", "find_events", "event_scout"),
        ("review my last diff", "review_code", "code_review"),
        ("what's due this week", "whats_due", "semester_planner"),
        ("plan my week", "plan_week", "semester_planner"),
        ("job status", "job_status", "job_hunter"),
        ("help me answer this form", "answer_form", "form_assist"),
    ],
)
def test_keyword_routing(router, text, expected_intent, expected_skill):
    intent = router.route(text, use_llm=False)
    assert intent.name == expected_intent
    assert intent.skill == expected_skill
    assert intent.via == "keyword"


def test_change_voice_extracts_arg(router):
    intent = router.route("change voice to zuri", use_llm=False)
    assert intent.name == "change_voice"
    assert intent.args["voice"] == "zuri"


def test_remember_extracts_instruction(router):
    intent = router.route("remember that I prefer cloud roles over transcription", use_llm=False)
    assert intent.name == "remember"
    assert "cloud roles" in intent.args["instruction"]


def test_research_extracts_query(router):
    intent = router.route("search for kubernetes operators tutorial", use_llm=False)
    assert intent.name == "research"
    assert intent.args["query"] == "kubernetes operators tutorial"


def test_approve_parses_numbers(router):
    intent = router.route("approve 1, 3 and 5", use_llm=False)
    assert intent.name == "approve"
    assert intent.args["selection"] == [1, 3, 5]


def test_tailor_cv_optional_target(router):
    intent = router.route("tailor my cv for the DevOps role at Acme", use_llm=False)
    assert intent.name == "tailor_cv"
    assert "Acme" in intent.args["target"]


def test_llm_fallback_used_when_no_keyword(fake_llm):
    fake_llm.classify_result = "find_jobs"
    router = IntentRouter(llm=fake_llm)
    intent = router.route("is there anything worth applying to today", use_llm=True)
    assert intent.via == "llm"
    assert intent.name == "find_jobs"
    assert intent.skill == "job_hunter"


def test_offline_mode_defaults_to_chit_chat(router):
    intent = router.route("mumble mumble nonsense phrase", use_llm=False)
    assert intent.name == "chit_chat"
    assert intent.via == "keyword"


# --- real classify() normalization (no network: subclass overrides _post) ---
def test_real_classify_normalizes_offmenu():
    class _C(LLMClient):
        def __init__(self, reply):
            self.routes = {"default": "d", "classify": "cheap"}
            self.defaults = {}
            self._reply = reply

        def _post(self, model, messages, **params):  # type: ignore[override]
            return self._reply

    assert _C("find_jobs").classify("x", ["find_jobs", "chit_chat"]) == "find_jobs"
    # trailing punctuation / case is tolerated
    assert _C("Chit_Chat.").classify("x", ["find_jobs", "chit_chat"]) == "chit_chat"
    # completely off-menu -> first label
    assert _C("banana").classify("x", ["find_jobs", "chit_chat"]) == "find_jobs"
