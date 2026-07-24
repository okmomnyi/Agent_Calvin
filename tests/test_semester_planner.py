"""Semester planner tests: deadline ranking, email extract→pending→confirm, timetable,
the unified briefing (sections + top-3), week plan, and cram (surge + MUST mock CAT PDF)."""

from __future__ import annotations

import json
import re
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


# ------------------------------------------------------------------ events (#21 regression)
# Report: the briefing's "📅 Events" line never changed day to day. Root cause: the query was
# "ORDER BY id DESC LIMIT 3" with no date filter at all -- once the 3 most-recently-marked
# events had all already happened, those same 3 stale titles kept showing forever.
def _add_event(mem, ext, title, date, *, status="interested"):
    mem.upsert_event("test", ext, title=title, fmt="online", date=date)
    row = mem.event_by_ref("test", ext)
    mem.set_event_status(row["id"], status)
    return row["id"]


def test_briefing_events_drop_off_once_they_have_passed(mem):
    from core.time_context import local_now

    today_iso = local_now(NOW).strftime("%Y-%m-%d")
    _add_event(mem, "past", "Old CTF", "2020-01-01")          # long over
    upcoming_id = _add_event(mem, "soon", "Cloud Meetup", today_iso)

    skill = _skill(mem)
    events = skill._upcoming_interested_events(NOW)

    assert [e["title"] for e in events] == ["Cloud Meetup"]
    assert upcoming_id  # sanity: the row really was created


def test_briefing_events_are_soonest_first(mem):
    _add_event(mem, "later", "DevOps Con", "2099-06-01")
    _add_event(mem, "sooner", "AWS Community Day", "2099-01-01")

    skill = _skill(mem)
    events = skill._upcoming_interested_events(NOW)

    assert [e["title"] for e in events] == ["AWS Community Day", "DevOps Con"]


def test_briefing_events_section_reflects_the_filtered_list(mem):
    _add_event(mem, "past", "Old CTF", "2020-01-01")
    _add_event(mem, "soon", "Cloud Meetup", "2099-01-01")

    skill = _skill(mem)
    res = skill.briefing(notify=False)

    assert "Cloud Meetup" in res.text
    assert "Old CTF" not in res.text


def test_briefing_events_with_no_parseable_date_still_show_but_sort_last(mem):
    _add_event(mem, "tba", "Mystery Hackathon", "")
    _add_event(mem, "dated", "AWS Community Day", "2099-01-01")

    skill = _skill(mem)
    events = skill._upcoming_interested_events(NOW)

    assert [e["title"] for e in events] == ["AWS Community Day", "Mystery Hackathon"]


def test_briefing_events_caps_at_three_soonest(mem):
    for i in range(5):
        _add_event(mem, f"ev{i}", f"Event {i}", f"2099-0{i + 1}-01")

    skill = _skill(mem)
    events = skill._upcoming_interested_events(NOW)

    assert [e["title"] for e in events] == ["Event 0", "Event 1", "Event 2"]


# ------------------------------------------------------------------ overdue sign bug (regression)
# The Telegram log showed a deadline OVERDUE by 2.5d reported by the Top-3 line as "due in 3
# days" -- days_left (correctly signed, negative once overdue) was interpolated raw into
# "in {dl:.0f}d" and handed to the LLM, which smoothed the confusing "-2d" into confident,
# wrong prose. Fixed by pre-formatting through relative_due() before it ever reaches the LLM
# or the no-LLM fallback, so there's no signed number left for either to misread.
def test_top3_llm_payload_never_carries_a_raw_signed_day_count(mem):
    class _RecordingLLM(_PlannerLLM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.last_messages = None

        def chat(self, task, messages, **kw):
            self.last_messages = messages
            return super().chat(task, messages, **kw)

    llm = _RecordingLLM()
    mem.add_deadline("OmniCTF 2026 Quals", NOW - 2.5 * 86400, unit="general", weight=1.0)
    skill = _skill(mem, llm)
    skill.briefing(notify=False)

    payload = json.loads(llm.last_messages[-1]["content"])
    entry = next(e for e in payload["top_deadlines"] if "OmniCTF" in e)
    assert "OVERDUE" in entry, f"overdue deadline lost its status in the LLM payload: {entry!r}"
    assert not re.search(r"\bin -?\d+d\b", entry), \
        f"a raw signed day count reached the LLM payload: {entry!r}"


def test_top3_fallback_reports_overdue_not_due_in(mem):
    class _DeadLLM(_PlannerLLM):
        def chat(self, task, messages, **kw):
            from core.llm import LLMError
            raise LLMError("down")

    mem.add_deadline("OmniCTF 2026 Quals", NOW - 2.5 * 86400, unit="general", weight=1.0)
    skill = _skill(mem, _DeadLLM())
    res = skill.briefing(notify=False)
    assert "OVERDUE" in res.text
    assert "due in" not in res.text.lower()


# ------------------------------------------------------------------ stale deadlines (regression, #13)
# Telegram log: the same 5 overdue deadlines appeared in all four briefings, counters
# climbing, including one titled "DownUnderCTF 2026 - CANCELLED" -- a cancelled event was
# still occupying a "next 7 days" slot, and nothing ever retired an overdue deadline the way
# job_hunter retires stale jobs.
def test_a_cancelled_titled_deadline_is_auto_retired(mem):
    mem.add_deadline("DownUnderCTF 2026 - CANCELLED", NOW - 5 * 86400, unit="general", weight=1.0)
    skill = _skill(mem)
    skill._retire_stale_deadlines()
    assert skill._ranked_deadlines(7) == []
    row = mem.execute("SELECT status FROM deadlines WHERE title=%s",
                      ("DownUnderCTF 2026 - CANCELLED",)).fetchone()
    assert row["status"] == "cancelled"


def test_a_deadline_overdue_past_the_staleness_window_is_auto_retired(mem):
    mem.add_deadline("Old assignment", NOW - 10 * 86400, unit="CS301", weight=1.0)
    skill = _skill(mem)
    skill._retire_stale_deadlines()
    assert skill._ranked_deadlines(7) == []
    row = mem.execute("SELECT status FROM deadlines WHERE title='Old assignment'").fetchone()
    assert row["status"] == "expired"


def test_a_recently_overdue_deadline_is_not_retired_yet(mem):
    """Still worth seeing -- only STALE overdue (>3 days) auto-retires."""
    mem.add_deadline("Just missed it", NOW - 1 * 86400, unit="CS301", weight=1.0)
    skill = _skill(mem)
    skill._retire_stale_deadlines()
    assert len(skill._ranked_deadlines(7)) == 1


def test_briefing_caps_overdue_display_and_still_shows_upcoming(mem):
    for i in range(4):
        mem.add_deadline(f"Overdue {i}", NOW - (1 + i * 0.1) * 86400, unit="CS301", weight=1.0)
    mem.add_deadline("Upcoming CAT", NOW + 2 * 86400, unit="CS305", weight=3.0)
    skill = _skill(mem)
    res = skill.briefing(notify=False)
    assert "more overdue" in res.text.lower()
    assert "Upcoming CAT" in res.text
    assert "⚠️ Overdue" in res.text and "🗓 Deadlines" in res.text


# ------------------------------------------------------------------ week planner (regression, #23)
# Telegram log: the same four generic blocks repeated across all seven days, even on days
# with real classes -- plan() only ever told the model which unit CODES exist
# (timetable.unit_names().keys()), never when they actually happen.
def test_weekly_schedule_summary_includes_day_and_time(tmp_path, monkeypatch):
    import yaml

    data = {"weekly": {"monday": [{"start": "08:00", "end": "10:00", "title": "Data Structures",
                                   "unit": "CS301"}],
                       "wednesday": [{"start": "14:00", "end": "16:00", "unit": "CS310"}]},
           "units": {"CS301": "Data Structures & Algorithms", "CS310": "Systems Analysis"}}
    p = tmp_path / "tt.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr("skills.semester_planner.timetable._path", lambda: p)

    skill = SemesterPlannerSkill(memory=None)
    lines = skill._weekly_schedule_summary()
    assert any("Monday 08:00-10:00: Data Structures" in ln for ln in lines)
    assert any("Wednesday 14:00-16:00: Systems Analysis" in ln for ln in lines)  # falls back to unit name


def test_plan_sends_the_real_weekly_schedule_to_the_model(mem, tmp_path, monkeypatch):
    import yaml

    data = {"weekly": {"tuesday": [{"start": "10:00", "end": "12:00", "title": "Automata Theory",
                                    "unit": "CS305"}]}, "units": {"CS305": "Automata Theory"}}
    p = tmp_path / "tt.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr("skills.semester_planner.timetable._path", lambda: p)

    class _RecordingLLM(_PlannerLLM):
        def __init__(self):
            super().__init__()
            self.last_messages = None

        def chat(self, task, messages, **kw):
            self.last_messages = messages
            return super().chat(task, messages, **kw)

    llm = _RecordingLLM()
    skill = _skill(mem, llm)
    skill.plan(notify=False)

    payload = json.loads(llm.last_messages[-1]["content"])
    assert any("Tuesday" in c and "Automata Theory" in c for c in payload["weekly_classes"])


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
