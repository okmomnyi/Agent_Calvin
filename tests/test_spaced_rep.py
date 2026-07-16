"""Spaced repetition tests: SM-2 progression, card lifecycle, due selection, the quiz
session machine, LLM answer-judging, weekly report, and exam-surge."""

from __future__ import annotations

from core.llm import LLMClient
from core.sm2 import CardState, schedule
from skills.spaced_rep import SpacedRepSkill


# ------------------------------------------------------------------ SM-2 algorithm
def test_sm2_first_reviews_and_growth():
    s = CardState()  # ease 2.5, interval 0
    s1 = schedule(s, "good")
    assert s1.interval_days == 1
    s2 = schedule(s1, "good")
    assert s2.interval_days == 6
    s3 = schedule(s2, "good")
    assert s3.interval_days == round(6 * s3.ease)  # grows by ease factor


def test_sm2_again_resets_and_counts_lapse():
    s = schedule(CardState(ease=2.5, interval_days=20, lapses=0), "again")
    assert s.interval_days == 1
    assert s.lapses == 1
    assert s.ease < 2.5              # ease penalised
    assert s.ease >= 1.3            # but floored


def test_sm2_easy_raises_ease_hard_shortens():
    easy = schedule(CardState(interval_days=10), "easy")
    hard = schedule(CardState(interval_days=10), "hard")
    assert easy.ease > 2.5
    assert hard.interval_days < easy.interval_days


# ------------------------------------------------------------------ card lifecycle
def _skill(mem, llm=None, now=1_000_000.0):
    return SpacedRepSkill(memory=mem, llm=llm, clock=lambda: now)


def test_approve_activates_candidate_and_makes_it_due(mem):
    mem.add_flashcard("Q?", "A", unit="CS", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    skill = _skill(mem)
    skill.approve_card(card_id=cid)
    card = mem.get_flashcard(cid)
    assert card["status"] == "active"
    assert len(mem.due_cards(now=1_000_000.0)) == 1


def test_reject_suspends_never_deletes(mem):
    mem.add_flashcard("Q?", "A", unit="CS", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    _skill(mem).reject_card(card_id=cid)
    assert mem.get_flashcard(cid)["status"] == "suspended"   # row still exists


def test_candidates_only_from_lecture_are_not_due(mem):
    mem.add_flashcard("Q?", "A", unit="CS", status="candidate")
    assert mem.due_cards(now=1_000_000.0) == []              # candidates aren't quizzed until approved


# ------------------------------------------------------------------ quiz session
def _active_deck(mem, now):
    for i in range(3):
        mem.add_flashcard(f"Q{i}?", f"A{i}", unit="CS", status="candidate")
    skill = _skill(mem, now=now)
    for c in mem.candidate_cards():
        skill.approve_card(card_id=c["id"])
    return skill


def test_quiz_session_runs_and_reschedules(mem):
    now = 2_000_000.0
    skill = _active_deck(mem, now)
    start = skill.quiz(unit="CS")
    assert start.data["total"] == 3 and "Q1" in start.text

    skill.reveal()
    r1 = skill.grade(grade="good")
    assert r1.data["done"] is False and "Q2" in r1.text
    skill.reveal(); skill.grade(grade="again")
    skill.reveal(); done = skill.grade(grade="easy")
    assert done.data["done"] is True and done.data["graded"] == 3

    # a 'good' card is no longer due now; reviews were logged
    assert len(mem.due_cards(now=now)) < 3
    assert mem.execute("SELECT COUNT(*) c FROM card_reviews").fetchone()["c"] == 3


def test_grade_requires_active_session(mem):
    assert _skill(mem).grade(grade="good").ok is False


class _JudgeLLM(LLMClient):
    def __init__(self, grade="good", feedback="Correct — good substance."):
        self.routes = {"default": "m", "classify": "m"}
        self.defaults = {}
        self._g, self._f = grade, feedback

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return {"grade": self._g, "feedback": self._f}


def test_voice_judge_grades_and_advances(mem):
    now = 3_000_000.0
    for i in range(2):
        mem.add_flashcard(f"Q{i}?", f"A{i}", unit="CS", status="candidate")
    skill = SpacedRepSkill(memory=mem, llm=_JudgeLLM(grade="good"), clock=lambda: now)
    for c in mem.candidate_cards():
        skill.approve_card(card_id=c["id"])
    skill.quiz(unit="CS")
    r = skill.quiz_answer(answer="my spoken answer")
    assert r.data["judged_grade"] == "good"
    assert mem.execute("SELECT COUNT(*) c FROM card_reviews").fetchone()["c"] == 1


# ------------------------------------------------------------------ report + surge
def test_report_computes_retention(mem):
    now = 4_000_000.0
    mem.add_flashcard("Q?", "A", unit="CS", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    skill = _skill(mem, now=now)
    skill.approve_card(card_id=cid)
    mem.log_review(cid, "CS", "good", now=now)
    mem.log_review(cid, "CS", "again", now=now)
    rep = skill.report(notify=False)
    assert rep.data["stats"]["CS"]["reviews"] == 2
    assert rep.data["stats"]["CS"]["retention"] == 0.5


def test_surge_brings_weak_cards_forward(mem):
    now = 5_000_000.0
    mem.add_flashcard("Q?", "A", unit="CS", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    skill = _skill(mem, now=now)
    skill.approve_card(card_id=cid)
    # push it far into the future, then give it a lapse so surge picks it up
    mem.update_card_schedule(cid, ease=2.0, interval_days=30, lapses=1, due_at=now + 30 * 86400)
    assert mem.due_cards(now=now) == []
    n = skill.surge(unit="CS").data["surged"]
    assert n == 1
    assert len(mem.due_cards(now=now)) == 1


def test_add_manual_card(mem):
    skill = _skill(mem)
    r = skill.add_card(front="What is TCP?", back="Transmission Control Protocol", unit="NET")
    assert r.data["added"] is True
    assert mem.count_flashcards(unit="NET", status="candidate") == 1
