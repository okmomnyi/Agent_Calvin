"""Cross-device session continuity (Phase 19).

The system runs on the VPS, so "any device" isn't a sync problem: one server-side session,
and every channel is a thin client into it. What must hold:
  * a turn from ANY channel lands in the SAME session, keyed to Calvin not a device;
  * moving device never leaves ambiguity — the handoff explicitly names the thread;
  * pending approvals are a cross-cutting, read-only view (it never approves anything).
"""

from __future__ import annotations

import pytest

from core.session import MAX_TURNS, SessionStore
from skills.session import SessionSkill

NOW = 1_800_000_000.0


@pytest.fixture
def store(mem):
    return SessionStore(memory=mem, clock=lambda: NOW)


@pytest.fixture
def skill(store):
    return SessionSkill(store=store, clock=lambda: NOW)


# ================================================================= one session, many channels
def test_empty_session_is_safe(store):
    s = store.get()
    assert s["turns"] == [] and s["last_channel"] is None and s["pending_approvals"] == []


def test_turns_from_different_channels_share_one_session(store):
    store.record_turn("any new jobs?", "3 matches", "telegram", "job_hunter")
    store.record_turn("quiz me", "Q1...", "voice", "spaced_rep")
    store.record_turn("what's waiting", "2 things", "dashboard", "session")
    s = store.get()
    assert [t["channel"] for t in s["turns"]] == ["telegram", "voice", "dashboard"]
    assert s["last_channel"] == "dashboard"          # keyed to Calvin, not a device
    assert s["session_id"] == "calvin"


def test_context_window_is_bounded(store):
    for i in range(MAX_TURNS + 6):
        store.record_turn(f"msg {i}", "ok", "cli", "chat")
    turns = store.get()["turns"]
    assert len(turns) == MAX_TURNS
    assert turns[-1]["text"] == f"msg {MAX_TURNS + 5}"   # newest kept


def test_live_skill_session_is_shared_across_channels(mem, store):
    assert store.live_skill_session() is None
    mem.kv_set("interview_prep.mock", '{"company":"Acme"}')
    assert store.live_skill_session() == "mock interview"   # visible from any channel


# ================================================================= explicit handoff
def test_handoff_names_the_thread_and_previous_channel(mem, store):
    store.record_turn("mock interview for Acme", "Q1. Tell me about yourself.", "telegram",
                      "interview_prep")
    mem.kv_set("interview_prep.mock", '{"company":"Acme"}')
    text = store.handoff_summary("voice")
    assert "telegram" in text                    # where it was
    assert "mock interview" in text              # what's live
    assert "Tell me about yourself" in text      # exactly where he left off


def test_handoff_with_nothing_in_progress_says_so(store):
    text = store.handoff_summary("dashboard")
    assert "Nothing in progress" in text         # never pretends to resume something


def test_handoff_resolves_device_aliases(store):
    """'from my phone' means Telegram — a matching claim shouldn't raise a warning."""
    store.record_turn("any jobs?", "2 matches", "telegram", "job_hunter")
    text = store.handoff_summary(claimed_from="phone")
    assert "telegram" in text
    assert "⚠️" not in text


def test_handoff_flags_a_claim_that_contradicts_the_record(store):
    """If he says 'from my phone' but the last activity was voice, say so — don't silently
    resume a different thread than he thinks."""
    store.record_turn("quiz me", "Q1...", "voice", "spaced_rep")
    text = store.handoff_summary(claimed_from="phone")
    assert "⚠️" in text
    assert "last activity was on **voice**" in text


def test_handoff_reports_elapsed_time(store, mem):
    store.record_turn("hi", "hello", "telegram", "chat")
    later = SessionStore(memory=mem, clock=lambda: NOW + 600)   # 10 minutes later
    assert "10 min ago" in later.handoff_summary("voice")


def test_handoff_skill_surfaces_the_summary(mem, skill):
    skill.store.record_turn("any jobs?", "2 matches", "telegram", "job_hunter")
    res = skill.handoff(channel="dashboard")
    assert res.data["handoff"] is True
    assert "telegram" in res.text


# ================================================================= pending approvals
def test_pending_approvals_spans_every_skill(mem, store):
    # a job awaiting approval
    mem.upsert_job("remoteok", "j1", title="DevOps", company="Acme")
    mem.set_job_status(mem.get_job_by_ref("remoteok", "j1")["id"], "notified")
    # a flip at the purchase gate
    mem.upsert_listing("jiji", "l1", title="iPhone 11", make_model="iphone 11",
                       asking_price=18000.0, listed_at=NOW)
    lid = mem.listing_by_ref("jiji", "l1")["id"]
    mem.set_state(lid, "PURCHASE_GATE", "seed")
    # a candidate flashcard, a pending deadline, a proposed rule
    mem.add_flashcard("What is a DFA?", "A deterministic finite automaton", unit="CS")
    mem.add_deadline("CAT 1", NOW + 86400, unit="CS305", status="pending")
    mem.log_signal("event_scout", "event_skipped", "Nairobi")
    sig = mem.execute("SELECT id FROM signal_log").fetchone()["id"]
    mem.set_signal_status(sig, "proposed")

    kinds = {a["kind"] for a in store.pending_approvals()}
    assert kinds == {"job", "flip", "flashcard", "deadline", "rule"}


def test_pending_approvals_is_read_only(mem, store):
    """Surfacing what's blocked must never approve anything."""
    mem.upsert_job("remoteok", "j2", title="SRE", company="Nimbus")
    jid = mem.get_job_by_ref("remoteok", "j2")["id"]
    mem.set_job_status(jid, "notified")
    store.pending_approvals()
    assert mem.get_job(jid)["status"] == "notified"     # untouched


def test_approvals_command_lists_them(mem, skill):
    mem.add_flashcard("Q?", "A", unit="CS")
    res = skill.approvals()
    assert res.data["pending"] and "flashcard" in res.text


def test_approvals_command_when_clear(skill):
    assert "Nothing is waiting on you" in skill.approvals().text


# ================================================================= status
def test_status_reports_channel_and_live_session(mem, skill):
    skill.store.record_turn("tutor drill trees", "Q...", "voice", "code_tutor")
    mem.kv_set("code_tutor.session", '{"mode":"drill"}')
    res = skill.status()
    assert res.data["last_channel"] == "voice"
    assert res.data["active_skill"] == "tutor session"


# ================================================================= contract
def test_session_skill_reads_no_instructions(skill):
    """Pure plumbing — no standing instruction should steer continuity."""
    assert skill.contract().reads_categories == []
