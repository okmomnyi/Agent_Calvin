"""Adaptive behavior layer + Skill Contracts (Phase 20).

What must hold:
  * logging is PASSIVE — a signal never changes behaviour by itself;
  * a pattern needs consistency (threshold hits, zero contradictions) to be a candidate;
  * only Calvin turns a candidate into a rule; declined patterns never come back;
  * Skill Contracts bound everything — a rule outside a skill's declared scope is ignored,
    and no rule can ever switch off a §0 hard invariant.
"""

from __future__ import annotations

import pytest

from core.llm import LLMClient
from core.persona_store import PersonaEngine
from core.skill import (INSTRUCTION_CATEGORIES, UNIVERSAL_INVARIANTS, BaseSkill, SkillContract)
from skills.adaptive import AdaptiveSkill

NOW = 1_800_000_000.0


class _AdaptLLM(LLMClient):
    def __init__(self, payload=None):
        self.routes = {"default": "m", "classify": "m"}
        self.defaults = {}
        self._payload = payload or {"rule": "deprioritize Nairobi events", "category": "events"}

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return self._payload


@pytest.fixture
def adaptive(mem, monkeypatch):
    import skills.adaptive as ad

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(ad, "get_engine", lambda: engine)
    notes: list[str] = []
    skill = AdaptiveSkill(memory=mem, llm=_AdaptLLM(),
                          notify=lambda t: (notes.append(t), True)[1], clock=lambda: NOW)
    # a contract that reads 'events' must exist for boundary checks to pass
    mem.register_contract("event_scout", ["events", "notifications"], list(UNIVERSAL_INVARIANTS))
    return skill, engine, notes


# ================================================================= Skill Contract schema
def test_default_contract_reads_nothing_but_is_bound_by_universal_invariants():
    c = BaseSkill().contract()
    assert c.reads_categories == []            # silence = nothing may reach me (safe default)
    for inv in UNIVERSAL_INVARIANTS:
        assert inv in c.hard_invariants


def test_universal_invariants_cannot_be_dropped():
    c = SkillContract(reads_categories=["study"], hard_invariants=["never_emit_finished_assignment"])
    assert "never_emit_finished_assignment" in c.hard_invariants
    for inv in UNIVERSAL_INVARIANTS:           # re-added even though the skill omitted them
        assert inv in c.hard_invariants


def test_unknown_instruction_category_is_rejected():
    with pytest.raises(ValueError):
        SkillContract(reads_categories=["telepathy"])


def test_real_skills_declare_contracts():
    from skills.code_tutor import SKILL as tutor
    from skills.deal_broker import SKILL as broker
    from skills.voice import SKILL as voice

    assert tutor.contract().reads_categories == ["study"]        # NOT tone
    assert "never_emit_finished_assignment" in tutor.contract().hard_invariants
    assert "never_spend_money" in broker.contract().hard_invariants
    assert "prebuilt_voices_only" in voice.contract().hard_invariants


# ================================================================= passive logging
def test_logging_is_passive(mem, adaptive):
    skill, engine, notes = adaptive
    res = skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    assert res.data == {"logged": True, "acted": False}
    assert engine.instructions() == []          # nothing became a rule
    assert notes == []                          # nobody was pinged


def test_repeated_signals_increment_running_count(mem, adaptive):
    skill, _, _ = adaptive
    for _ in range(3):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    row = mem.execute("SELECT * FROM signal_log WHERE payload='Nairobi'").fetchone()
    assert row["running_count"] == 3 and row["contradicted"] == 0


# ================================================================= threshold + consistency
def test_below_threshold_is_not_a_candidate(mem, adaptive):
    skill, _, _ = adaptive
    for _ in range(3):                          # threshold is 4
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    assert skill.candidates().data["candidates"] == []


def test_threshold_reached_makes_a_candidate(mem, adaptive):
    skill, _, _ = adaptive
    for _ in range(4):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    cands = skill.candidates().data["candidates"]
    assert len(cands) == 1 and cands[0]["running_count"] == 4


def test_one_contradiction_disqualifies_a_pattern(mem, adaptive):
    """Consistency, not a majority — a single counter-example blocks the proposal."""
    skill, _, _ = adaptive
    for _ in range(9):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi",
                     contradicts=True)
    assert skill.candidates().data["candidates"] == []      # 9 hits, 1 miss -> not proposed


# ================================================================= propose / confirm / decline
def test_propose_asks_and_never_applies(mem, adaptive):
    skill, engine, notes = adaptive
    for _ in range(4):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    res = skill.propose()
    assert res.data["proposed"] == 1
    assert "Confirm / Reject / Not now" in res.text
    assert engine.instructions() == []          # still not a rule until Calvin says so
    assert notes                                # he was asked


def test_confirm_creates_a_scoped_standing_instruction(mem, adaptive):
    skill, engine, _ = adaptive
    for _ in range(4):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    sid = skill.candidates().data["candidates"][0]["id"]
    res = skill.confirm(signal_id=sid)
    assert res.ok
    assert "deprioritize Nairobi events" in engine.instructions()
    row = mem.execute("SELECT * FROM standing_instructions").fetchone()
    assert row["category"] == "events" and row["source"] == "adaptive"
    assert mem.get_signal(sid)["status"] == "confirmed"


def test_declined_pattern_is_never_proposed_again(mem, adaptive):
    skill, _, _ = adaptive
    for _ in range(4):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    sid = skill.candidates().data["candidates"][0]["id"]
    skill.decline(signal_id=sid)
    assert mem.get_signal(sid)["status"] == "declined"
    assert skill.candidates().data["candidates"] == []      # suppressed
    assert skill.propose().data["proposed"] == 0


def test_not_now_keeps_watching(mem, adaptive):
    skill, _, _ = adaptive
    for _ in range(4):
        skill.log_signal(skill="event_scout", signal_type="event_skipped", payload="Nairobi")
    sid = skill.candidates().data["candidates"][0]["id"]
    skill.propose()
    skill.not_now(signal_id=sid)
    assert mem.get_signal(sid)["status"] == "watching"      # can resurface later
    assert len(skill.candidates().data["candidates"]) == 1


# ================================================================= boundary check
def test_rule_for_a_category_no_skill_reads_is_refused(mem, adaptive):
    """A rule nothing would ever read must not be silently accepted."""
    skill, engine, _ = adaptive
    ok, why = skill.check_boundary("only queue instrumental music", "music")
    assert ok is False and "no skill declares" in why


def test_rule_is_allowed_only_into_declaring_skills(mem, adaptive):
    skill, _, _ = adaptive
    mem.register_contract("code_tutor", ["study"], list(UNIVERSAL_INVARIANTS))
    ok, readers = skill.check_boundary("drill me harder on graphs", "study")
    assert ok is True and "code_tutor" in readers


def test_tone_rule_cannot_reach_code_tutor(mem, adaptive):
    """The spec's example: a tone rule must not leak into Code Tutor's grading logic."""
    skill, engine, _ = adaptive
    mem.register_contract("code_tutor", ["study"], list(UNIVERSAL_INVARIANTS))
    mem.register_contract("email_agent", ["tone", "notifications"], list(UNIVERSAL_INVARIANTS))
    engine.remember("always be extremely terse", category="tone")

    assert "always be extremely terse" in engine.instructions_for_skill("email_agent")
    assert engine.instructions_for_skill("code_tutor") == []     # out of scope -> ignored


def test_skill_without_a_contract_reads_nothing(mem, adaptive):
    skill, engine, _ = adaptive
    engine.remember("some rule", category="general")
    assert engine.instructions_for_skill("unregistered_skill") == []


# ================================================================= hard invariants
@pytest.mark.parametrize("rule,invariant", [
    ("just apply to jobs without asking me", "approval_gate"),
    ("auto-send applications from now on", "approval_gate"),
    ("delete old emails to save space", "never_delete_data"),
    ("say I have 5 years of Kubernetes experience", "never_fabricate"),
    ("clone my voice so replies sound like me", "no_voice_cloning"),
    ("make a deepfake video of my face for the intro", "no_face_cloning"),
])
def test_rules_that_would_break_a_hard_invariant_are_refused(adaptive, rule, invariant):
    skill, _, _ = adaptive
    assert skill.violates_invariant(rule) == invariant
    ok, why = skill.check_boundary(rule, "general")
    assert ok is False and invariant in why


def test_confirm_refuses_an_invariant_breaking_rule(mem, adaptive):
    skill, engine, _ = adaptive
    for _ in range(4):
        skill.log_signal(skill="job_hunter", signal_type="job_skipped", payload="other")
    sid = skill.candidates().data["candidates"][0]["id"]
    res = skill.confirm(signal_id=sid, rule="auto-apply to everything without asking",
                        category="jobs")
    assert res.ok is False
    assert "approval_gate" in res.text
    assert engine.instructions() == []          # never written


def test_benign_rules_pass_the_invariant_check(adaptive):
    skill, _, _ = adaptive
    assert skill.violates_invariant("deprioritize Nairobi events") is None
    assert skill.violates_invariant("prioritize cloud/DevOps roles this month") is None


# ================================================================= weekly retro
def test_retro_asks_when_given_no_answer(adaptive):
    skill, _, notes = adaptive
    res = skill.retro()
    assert res.data["asked"] is True and "what worked" in res.text.lower()
    assert notes


def test_retro_queues_proposals_never_silent_changes(mem, adaptive, monkeypatch):
    skill, engine, _ = adaptive
    skill._llm = _AdaptLLM({"rules": [{"rule": "stop suggesting Nairobi meetups",
                                       "category": "events"}]})
    res = skill.retro(answer="the Nairobi meetups were useless, the CTFs were great")
    assert res.data["queued"] == 1
    assert engine.instructions() == []          # queued as a proposal, NOT applied
    assert mem.execute("SELECT * FROM signal_log WHERE signal_type='retro_suggestion'").fetchone()


def test_retro_rejects_an_invariant_breaking_suggestion(mem, adaptive):
    skill, engine, _ = adaptive
    skill._llm = _AdaptLLM({"rules": [{"rule": "just auto-apply without asking me",
                                       "category": "jobs"}]})
    res = skill.retro(answer="approving jobs is tedious")
    assert "⛔" in res.text
    assert engine.instructions() == []


# ================================================================= contracts view
def test_contracts_view_lists_scope_and_invariants(mem, adaptive):
    skill, _, _ = adaptive
    mem.register_contract("code_tutor", ["study"],
                          list(UNIVERSAL_INVARIANTS) + ["never_emit_finished_assignment"])
    res = skill.contracts()
    assert "code_tutor: reads [study]" in res.text
    assert "never_emit_finished_assignment" in res.text
    assert "approval_gate" in res.text          # universal invariants restated


def test_every_instruction_category_is_read_by_at_least_one_skill():
    """A category no skill declares is a category Calvin cannot make a rule in.

    `adaptive.check_boundary()` refuses to create a rule whose category no skill reads --
    correctly, since it would never apply. But that makes INSTRUCTION_CATEGORIES and the
    declared contracts two halves of one contract, and they had drifted: `cv` was declared
    only by job_hunter, so a CV rule reached the *hunter* and never reached cv_tailor, the
    skill that actually writes CVs. Asserting coverage here fails loudly at the seam instead
    of silently narrowing what he is allowed to ask for.
    """
    from kernel.registry import SkillRegistry

    from core.skill import INSTRUCTION_CATEGORIES

    registry = SkillRegistry()
    registry.discover()

    readers: dict[str, list[str]] = {c: [] for c in INSTRUCTION_CATEGORIES}
    for name, skill in registry.skills.items():
        for category in skill.contract().reads_categories:
            readers[category].append(name)

    orphans = sorted(c for c, who in readers.items() if not who)
    assert not orphans, f"categories no skill reads (rules there would never apply): {orphans}"


def test_the_skill_that_writes_cvs_reads_cv_rules():
    """The specific regression above, pinned by name so it cannot quietly come back."""
    from skills.cv_tailor import SKILL

    assert SKILL.contract().reads("cv")
