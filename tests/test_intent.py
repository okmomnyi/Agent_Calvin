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


@pytest.mark.parametrize("text", [
    "refine my CV",
    "improve my resume",
    "polish my CV for a cloud engineer role",
])
def test_refine_cv_natural_phrasings(router, text):
    intent = router.route(text, use_llm=False)
    assert intent.name == "refine_cv"
    assert intent.skill == "cv_tailor"
    if "cloud engineer" in text:
        assert intent.args["target"] == "a cloud engineer role"


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


# ============================================================ speech, not typing
@pytest.mark.parametrize("said,skill", [
    # Whisper's punctuation is its own opinion: identical audio comes back with a straight
    # apostrophe, a curly one, or none at all. Calvin said "Whats due this week" and got a
    # list of CTF events, because the rule needed U+0027 and the LLM fallback guessed.
    ("Whats due this week", "semester_planner"),
    ("what's due this week", "semester_planner"),
    ("what’s due this week", "semester_planner"),      # curly, whisper's favourite
    ("what is due this week", "semester_planner"),
    ("whats due", "semester_planner"),
    ("my deadlines", "semester_planner"),
    ("whats my session", "session"),
    ("what’s my session", "session"),
    ("whats waiting on me", "session"),
])
def test_apostrophes_never_decide_the_intent(said, skill):
    assert IntentRouter(llm=None).route(said, use_llm=False).skill == skill


def test_due_is_not_confused_with_events():
    """The exact misroute Calvin hit: deadlines -> a list of CTFs."""
    r = IntentRouter(llm=None)
    assert r.route("Whats due this week", use_llm=False).skill == "semester_planner"
    assert r.route("any free events", use_llm=False).skill == "event_scout"


def test_email_trash_and_restore_intents(router):
    from skills.email_agent import _clean_delete_query

    trash = router.route("delete some of my emails", use_llm=False)
    assert trash.name == "trash_email" and trash.skill == "email_agent"
    # A bare "delete some of my emails" carries no real target: whatever is captured must
    # clean to empty, so request_trash asks which ones instead of trashing everything.
    assert _clean_delete_query(trash.args.get("query", "")) == ""
    filtered = router.route("delete my emails from LinkedIn", use_llm=False)
    assert "linkedin" in _clean_delete_query(filtered.args["query"]).lower()
    restore = router.route("undo email trash", use_llm=False)
    assert restore.name == "restore_email"


def test_real_delete_phrasings_route_to_trash(router):
    """The phrasings Calvin actually used, which the old rule missed and sent to 'tutor'."""
    from skills.email_agent import _clean_delete_query, _gmail_query

    cases = {
        "Can you delete all the emails related to okx that are not transactional": "okx",
        "delete all linkedin emails": "linkedin",
        "clear all facebook emails": "facebook",
        "get rid of indie games emails": "indie games",
    }
    for utterance, expected_entity in cases.items():
        got = router.route(utterance, use_llm=False)
        assert got.skill == "email_agent" and got.name == "trash_email", utterance
        q = _clean_delete_query(got.args.get("query", ""))
        assert expected_entity in q.lower(), f"{utterance!r} -> {q!r}"
    # promotional maps to Gmail's category, not a literal text search
    promo = router.route("delete promotional emails", use_llm=False)
    assert _gmail_query(_clean_delete_query(promo.args["query"])) == "category:promotions"


def test_event_action_intents_extract_exact_id(router):
    interested = router.route("interested in event 12", use_llm=False)
    assert interested.name == "event_interested"
    assert interested.args["event_id"] == "12"


def test_current_time_never_uses_llm_fallback(router):
    intent = router.route("what time is it right now?", use_llm=True)
    assert intent.name == "current_time"
    assert intent.skill == "chat"
    assert intent.via == "keyword"


def test_no_rule_contains_a_literal_control_character():
    """A regex written through a shell heredoc can pick up REAL control characters.

    `\b` outside a raw string becomes backspace (0x08), which compiles fine and then never
    matches anything — the rule is silently dead. That is exactly what happened to the
    music_budget rule: present, imported, and incapable of matching its own examples.
    """
    from core.intent import _RULES

    bad = [name for name, pattern, _ in _RULES
           if any(ord(ch) < 32 and ch not in "\t" for ch in pattern.pattern)]
    assert not bad, f"rules contain literal control chars (dead regexes): {bad}"


def test_every_intent_rule_maps_to_a_registered_intent():
    """A rule whose name is missing from INTENTS silently falls back to chit_chat."""
    from core.intent import _RULES, INTENTS

    unknown = sorted({name for name, _, _ in _RULES} - set(INTENTS))
    assert not unknown, f"rules with no INTENTS entry (they route to chat): {unknown}"


# ================================================ keyword re-entry (one-shot sessions)
# Calvin named these keywords by hand: "if i say tutor the tutor session is started ...
# when i say create a playlist or remove from playlist thats for music". With sessions now
# one-shot, the keyword IS the way back in -- so it routes deterministically, not on a guess.
KEYWORDS = [
    ("tutor", "code_tutor", "start"),
    ("tutor me on kubernetes", "code_tutor", "start"),
    ("quiz me", "spaced_rep", "quiz"),
    ("mock interview", "interview_prep", "mock"),
    ("create a playlist for coding", "music", "playlist"),
    ("make me a playlist called deep focus", "music", "playlist"),
    ("remove from playlist", "music", "playlist_remove"),
    ("remove Fireworks from my coding playlist", "music", "playlist_remove"),
]


@pytest.mark.parametrize("text,skill,action", KEYWORDS)
def test_keyword_reentry(text, skill, action):
    intent = IntentRouter(llm=None).route(text, use_llm=False)
    assert (intent.skill, intent.action) == (skill, action), f"{text!r} misrouted"


def test_playlist_removal_never_routes_to_email_trash():
    """The trash rule is a catch-all on the verb "remove", so "remove X from my playlist" was
    one confirmation away from binning email. This is a data-loss guard, not a nicety."""
    for text in ["remove from playlist", "remove Fireworks from my playlist",
                 "delete that track from my playlist", "clear the song from my playlist"]:
        intent = IntentRouter(llm=None).route(text, use_llm=False)
        assert intent.skill != "email_agent", f"{text!r} routed to EMAIL: {intent.action}"


def test_email_deletion_still_works():
    """...and the guard must not cost him the email clearing he uses daily."""
    for text in ["clear my emails", "delete all linkedin emails", "get rid of the okx emails"]:
        intent = IntentRouter(llm=None).route(text, use_llm=False)
        assert (intent.skill, intent.action) == ("email_agent", "trash"), text
