"""Semester planner tests: deadline ranking, email extract→pending→confirm, timetable,
the unified briefing (sections + top-3), week plan, and cram (surge + MUST mock CAT PDF)."""

from __future__ import annotations

from pathlib import Path

from unittest.mock import MagicMock

from core import timetable
from core.llm import LLMClient
from core.time_context import local_now, parse_local_datetime
from skills.semester_planner import SemesterPlannerSkill, _iso_to_epoch


NOW = 1_800_000_000.0  # fixed clock


class _PlannerLLM(LLMClient):
    def __init__(self, chat_text="1. Do X\n2. Do Y\n3. Do Z", json_payload=None):
        self.routes = {"default": "m", "write": "m", "classify": "m"}
        self.defaults = {}
        self._chat = chat_text
        self._json = json_payload or {}

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return self._chat

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return self._json


def _skill(mem, llm=None, now=NOW, notify=None):
    # notify injected: extract_deadlines() notifies unconditionally, and without this the
    # suite texted Calvin about a fixture deadline on every run.
    return SemesterPlannerSkill(memory=mem, llm=llm or _PlannerLLM(), clock=lambda: now,
                               notify=notify or MagicMock())


# ------------------------------------------------------------------ deadlines
def test_iso_to_epoch():
    epoch = _iso_to_epoch("2026-08-01")
    assert epoch is not None
    assert local_now(epoch).strftime("%H:%M:%S") == "23:59:59"
    assert _iso_to_epoch("not a date") is None


def test_deadline_add_and_ranking_by_urgency_x_weight(mem):
    skill = _skill(mem)
    # near, low weight
    mem.add_deadline("Lab report", NOW + 2 * 86400, unit="CS320", dtype="lab", weight=1.0)
    # slightly further, high weight -> should still rank near top
    mem.add_deadline("Midterm exam", NOW + 3 * 86400, unit="CS301", dtype="exam", weight=5.0)
    # far away
    mem.add_deadline("Essay", NOW + 6 * 86400, unit="CS310", dtype="assignment", weight=1.0)
    ranked = skill._ranked_deadlines(7)
    assert ranked[0][0]["title"] == "Midterm exam"    # weight dominates
    assert len(ranked) == 3


def test_email_extract_creates_pending_then_confirm(mem):
    llm = _PlannerLLM(json_payload={"deadlines": [
        {"title": "CAT 1", "unit": "CS305", "type": "CAT", "due_date": "2027-06-01", "weight": 2.0}]})
    skill = _skill(mem, llm)
    msgs = [{"sender": "lecturer@must.ac.ke", "subject": "CAT 1 next week", "snippet": "on 15 Jan"}]
    res = skill.extract_deadlines(messages=msgs)
    assert res.data["pending"] == 1
    pend = mem.pending_deadlines()
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    # confirm activates it
    skill.confirm_deadline(deadline_id=pend[0]["id"])
    assert mem.pending_deadlines() == []
    assert len(mem.deadlines_within(400, now=NOW)) == 1


# ------------------------------------------------------------------ timetable
def test_timetable_classes_on(tmp_path):
    import yaml

    data = {"weekly": {"monday": [{"start": "08:00", "title": "DSA", "unit": "CS301"}]},
            "units": {"CS301": "Data Structures"},
            "one_off": [{"date": "2026-07-20", "start": "14:00", "title": "Guest lecture"}]}
    p = tmp_path / "tt.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    loaded = timetable.load(p)
    monday = timetable.classes_on(0, "", loaded)   # 0 = Monday
    assert monday[0]["title"] == "DSA"
    # one-off merged on its date (Tuesday index 1)
    with_oneoff = timetable.classes_on(1, "2026-07-20", loaded)
    assert any(c["title"] == "Guest lecture" for c in with_oneoff)


# ------------------------------------------------------------------ briefing
def test_briefing_assembles_sections_and_top3(mem, monkeypatch):
    # deadline + due card + job approval + interview -> all surfaced
    mem.add_deadline("Project demo", NOW + 1 * 86400, unit="CS320", dtype="assignment", weight=3.0)
    mem.add_flashcard("Q?", "A", unit="CS301", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    mem.approve_card(cid, now=NOW)
    mem.upsert_job("remoteok", "j1", title="DevOps", company="Acme")
    jid = mem.get_job_by_ref("remoteok", "j1")["id"]
    mem.set_job_status(jid, "notified")
    mem.record_application(job_id=None, company="CloudCorp", source="x", category="cloud_devops")
    mem.set_application_status(1, "interview")

    llm = _PlannerLLM(chat_text="1. Finish project demo (due tomorrow)\n2. Clear flashcards\n3. Approve Acme job")
    skill = _skill(mem, llm)
    res = skill.briefing(notify=False)
    t = res.text
    assert "Top 3 today" in t
    assert "Project demo" in t
    assert res.data["job_approvals"] == 1
    assert res.data["cards_due"] == 1
    assert "CloudCorp" in t   # interview surfaced


def test_briefing_top3_heuristic_without_llm(mem):
    class _DeadLLM(_PlannerLLM):
        def chat(self, task, messages, **kw):
            from core.llm import LLMError
            raise LLMError("down")

    mem.add_deadline("Exam", NOW + 2 * 86400, unit="CS305", dtype="exam", weight=5.0)
    skill = _skill(mem, _DeadLLM())
    res = skill.briefing(notify=False)
    assert "Exam" in res.text   # heuristic top-3 still lists the nearest deadline


def test_briefing_uses_actual_local_time_not_hardcoded_morning(mem):
    afternoon = parse_local_datetime("2026-07-18T14:30")
    skill = _skill(mem, now=afternoon)
    result = skill.briefing(notify=False)
    assert "Good afternoon" in result.text
    assert "Local time is 14:30" in result.text
    assert "Morning" not in result.text


def test_due_surfaces_overdue_deadlines(mem):
    mem.add_deadline("Missed lab", NOW - 3600, unit="CS320", dtype="lab", weight=1.0)
    result = _skill(mem).due(days=7)
    assert "OVERDUE" in result.text
    assert "Missed lab" in result.text


# ------------------------------------------------------------------ plan
def test_plan_saves_proposal(mem):
    skill = _skill(mem, _PlannerLLM(chat_text="Mon: study CS301..."))
    res = skill.plan(notify=False)
    assert "study CS301" in res.text
    assert mem.kv_get("semester.plan")   # persisted for briefings to reference


# ------------------------------------------------------------------ cram
def test_cram_surges_and_writes_mock_cat_pdf(mem, tmp_path, monkeypatch):
    import skills.semester_planner as sp

    real = sp.get_settings()

    class _S:
        def __init__(self): self.data_dir = tmp_path
        def __getattr__(self, n): return getattr(real, n)

    monkeypatch.setattr(sp, "get_settings", lambda: _S())

    # a weak active card in the unit so surge has something to move
    mem.add_flashcard("Q?", "A", unit="CS305", status="candidate")
    cid = mem.candidate_cards()[0]["id"]
    mem.approve_card(cid, now=NOW)
    mem.update_card_schedule(cid, ease=2.0, interval_days=30, lapses=1, due_at=NOW + 30 * 86400)

    cat = {"questions": [{"q": "Define a DFA", "marks": 10, "answer": "5-tuple ..."},
                         {"q": "NFA to DFA?", "marks": 20, "answer": "subset construction"}], "total": 30}
    skill = _skill(mem, _PlannerLLM(chat_text="Day 1: DFA basics", json_payload=cat))
    res = skill.cram(unit="CS305", days=3, notify=False)
    assert res.data["surged"] == 1
    paper = Path(res.data["cat_pdf"])
    scheme = Path(res.data["marking_scheme"])
    assert paper.exists() and paper.stat().st_size > 800
    assert scheme.exists()                      # marking scheme generated separately
    # marking scheme is revealed only via cram_marking (after attempting)
    assert "Marking scheme" in skill.cram_marking(unit="CS305").text
