"""Outbound calls + pickup/hangup (Phase 36 Slice 5).

The properties that matter most: a call CANNOT be placed without an explicit second
confirmation, approving one never makes the next automatic (`high` is not learnable — see
kernel/registry.py's `_ACTION_TIERS` and core/approvals.py's `LEARNABLE_TIERS`), an
ambiguous contact name asks and dispatches nothing, and a malformed/tampered number never
reaches a client action.
"""

from __future__ import annotations

import json

import pytest

from core.approvals import LEARNABLE_TIERS, TIER_HIGH, TIER_LOW
from kernel.registry import SkillRegistry
from skills.contacts import ContactsSkill
from skills.phone import PhoneSkill, _CALL_SESSION_KEY


@pytest.fixture
def contacts(mem):
    return ContactsSkill(memory=mem)


@pytest.fixture
def phone(mem, contacts):
    return PhoneSkill(memory=mem, contacts=contacts)


# ================================================================= tiers (never learnable)
def test_placing_a_call_is_high_tier():
    registry = SkillRegistry()
    assert registry.action_tier("phone", "call") == TIER_HIGH


def test_high_tier_is_structurally_excluded_from_learning():
    """The build prompt's own guardrail: approving one call must never make the next
    automatic. This is enforced by `high` never appearing in LEARNABLE_TIERS at all —
    there is no code path, correct or buggy, that can teach it "always yes"."""
    assert TIER_HIGH not in LEARNABLE_TIERS


def test_answering_and_hanging_up_are_low_tier_and_learnable():
    registry = SkillRegistry()
    assert registry.action_tier("phone", "answer") == TIER_LOW
    assert registry.action_tier("phone", "hangup") == TIER_LOW
    assert TIER_LOW in LEARNABLE_TIERS


def test_a_spoofed_tier_on_a_plan_step_is_overwritten_by_the_server(mem):
    """A goal plan step arrives with whatever tier the model or a client claims; the
    orchestrator's own validation pass always overwrites it from kernel/registry.py's
    table before anything is dispatched or gated on it."""
    from core.orchestrator import Orchestrator, Plan, PlanStep

    registry = SkillRegistry()
    registry.discover()
    orch = Orchestrator(registry=registry, memory=mem)
    spoofed = PlanStep(id="s1", skill="phone", action="call", args={"name": "Mum"},
                       tier="trivial")
    plan = Plan(id="test-phone-plan", goal="call mum", steps=[spoofed])
    validated = orch.validate_plan(plan)
    assert validated.steps[0].tier == "high", \
        "the server's own tier table must win over whatever a step claimed"


# ================================================================= call cannot execute without approval
def test_call_never_executes_on_the_first_step(phone, contacts):
    contacts.add(name="Mum", phone="0712345678")
    result = phone.call(name="Mum")
    assert result.ok is True
    assert result.data["requires_confirmation"] is True
    assert "client_actions" not in result.data, "the preview step must not place the call"


def test_call_preview_shows_the_resolved_name_and_full_number(phone, contacts):
    contacts.add(name="Mum", phone="0712345678")
    result = phone.call(name="Mum")
    assert result.data["call_preview"] == {"name": "Mum", "number": "+254712345678"}
    assert "+254712345678" in result.text


def test_confirming_places_exactly_one_call(phone, contacts):
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    result = phone.continue_call(text="confirm call")
    assert result.data["client_actions"] == [{"op": "call", "number": "+254712345678"}]


def test_approving_one_call_does_not_make_the_next_automatic(phone, contacts):
    """The exact scenario the build prompt calls out: place one call, then ask again --
    the second attempt must produce a fresh, unconfirmed preview, not an instant call."""
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    phone.continue_call(text="confirm call")  # first call placed

    second = phone.call(name="Mum")
    assert second.data["requires_confirmation"] is True
    assert "client_actions" not in second.data, \
        "a second 'call mum' must ask again, not place the call immediately"


def test_cancelling_places_no_call(phone, contacts):
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    result = phone.continue_call(text="cancel")
    assert "client_actions" not in result.data
    assert "cancel" in result.text.lower()


def test_an_unrecognized_reply_also_cancels_rather_than_placing_the_call(phone, contacts):
    """Fail closed: anything that isn't a clear confirmation must not place a call."""
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    result = phone.continue_call(text="what's the weather")
    assert "client_actions" not in result.data


def test_confirming_with_nothing_pending_is_reported_not_a_crash(phone):
    result = phone.continue_call(text="confirm call")
    assert result.ok is False
    assert "no call" in result.text.lower()


def test_session_is_cleared_after_confirmation_so_a_stray_reply_cannot_re_trigger_it(phone, contacts, mem):
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    phone.continue_call(text="confirm call")
    assert mem.kv_get(_CALL_SESSION_KEY) in (None, "")
    replay = phone.continue_call(text="confirm call")
    assert "client_actions" not in replay.data


# ================================================================= ambiguity: asks, dispatches nothing
def test_ambiguous_contact_name_asks_and_dispatches_nothing(phone, contacts, mem):
    contacts.add(name="John Doe", phone="0712345678")
    contacts.add(name="John Smith", phone="0712345679")
    result = phone.call(name="John")
    assert result.ok is False
    assert result.data.get("ambiguous") is True
    assert "client_actions" not in result.data
    assert mem.kv_get(_CALL_SESSION_KEY) in (None, ""), \
        "an ambiguous name must not even start a pending call session"


def test_unknown_contact_name_asks_and_dispatches_nothing(phone, contacts):
    result = phone.call(name="Nobody At All")
    assert result.ok is False
    assert "client_actions" not in result.data


def test_contact_with_no_number_on_file_refuses_cleanly(phone, contacts):
    contacts.add(name="No Number", email="x@example.com")
    result = phone.call(name="No Number")
    assert result.ok is False
    assert "client_actions" not in result.data


def test_calling_with_no_name_asks_rather_than_guessing(phone):
    result = phone.call(name="")
    assert result.ok is False


# ================================================================= malformed number never reaches a client action
def test_a_tampered_session_number_is_refused_at_confirmation(phone, contacts, mem):
    """Simulates the session payload being corrupted between preview and confirm -- the
    second validation pass (skills/contacts.normalize_phone) must still catch it."""
    contacts.add(name="Mum", phone="0712345678")
    phone.call(name="Mum")
    mem.kv_set(_CALL_SESSION_KEY, json.dumps(
        {"name": "Mum", "number": "'; rm -rf /", "created_at": 0}))
    result = phone.continue_call(text="confirm call")
    assert result.ok is False
    assert "client_actions" not in result.data


# ================================================================= answer / hangup
def test_answer_emits_the_answer_op(phone):
    result = phone.answer()
    assert result.data["client_actions"] == [{"op": "answer"}]


def test_hangup_emits_the_hangup_op(phone):
    result = phone.hangup()
    assert result.data["client_actions"] == [{"op": "hangup"}]


# ================================================================= planner exclusion
def test_continue_call_is_excluded_from_the_planner_manifest():
    """continue_call only ever makes sense as a direct reply to the preview `call` produced
    -- never a freestanding planned/catalogue-picked action."""
    registry = SkillRegistry()
    registry.discover()
    manifest_actions = {(m["skill"], m["action"]) for m in registry.manifest()}
    assert ("phone", "continue_call") not in manifest_actions
    assert ("phone", "call") in manifest_actions
