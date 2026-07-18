"""Tiered actions + learned permissions (Phase 30).

Adopted from OpenJarvis's proactive-agent model. The tests that matter are the ones pinning
where AgentOS deliberately diverges:

  * `high` tier (acting in Calvin's voice) can NEVER be auto-approved, even by "always yes".
    §0 P3 is not a preference the agent gets to learn away.
  * A remembered decision applies to its OWN action type only -- "always trash LinkedIn"
    must never become "always reply to LinkedIn".
  * Nothing is deleted; a revoked permission goes back to ASK (§0 P4).
"""

from __future__ import annotations

import pytest

from core.approvals import (ALWAYS_APPROVE, ALWAYS_DENY, APPROVED, ASK, DENIED, PENDING,
                            TIER_HIGH, TIER_LOW, TIER_MEDIUM, TIER_TRIVIAL, ApprovalStore,
                            parse_approval_reply)


@pytest.fixture
def store(mem):
    return ApprovalStore(memory=mem, clock=lambda: 1_000.0)


# ================================================================= tiers
def test_trivial_actions_need_no_approval(store):
    """Read-only/categorisation has no external effect -- gating it is just noise."""
    _id, status = store.propose("email_classify", "Label as newsletter", tier=TIER_TRIVIAL,
                                permission_key="email_classify:from:x.com")
    assert status == APPROVED


def test_low_and_medium_ask_the_first_time(store):
    for tier in (TIER_LOW, TIER_MEDIUM):
        _id, status = store.propose("email_trash", f"Trash a {tier} thing", tier=tier,
                                    permission_key=f"email_trash:from:{tier}.com")
        assert status == PENDING


def test_high_tier_always_asks(store):
    _id, status = store.propose("job_apply", "Apply to Acme as Calvin", tier=TIER_HIGH,
                                permission_key="job_apply:company:acme")
    assert status == PENDING


# ================================================================= learning
def test_always_yes_stops_it_asking_again(store):
    """The whole point: triage the same sender once, not every week."""
    first, status = store.propose("email_trash", "Trash LinkedIn promo", tier=TIER_LOW,
                                  permission_key="email_trash:from:linkedin.com")
    assert status == PENDING
    store.resolve(first, approve=True, always=True)

    _id2, status2 = store.propose("email_trash", "Trash another LinkedIn promo", tier=TIER_LOW,
                                  permission_key="email_trash:from:linkedin.com")
    assert status2 == APPROVED, "it asked again about a pattern he already settled"


def test_always_no_skips_silently(store):
    first, _ = store.propose("email_trash", "Trash bank statement", tier=TIER_LOW,
                             permission_key="email_trash:from:bank.com")
    store.resolve(first, approve=False, always=True)
    _id2, status2 = store.propose("email_trash", "Trash another statement", tier=TIER_LOW,
                                  permission_key="email_trash:from:bank.com")
    assert status2 == DENIED


def test_a_learned_yes_never_crosses_action_types(store):
    """'always trash LinkedIn' must not become 'always reply to LinkedIn'."""
    first, _ = store.propose("email_trash", "Trash LinkedIn promo", tier=TIER_LOW,
                             permission_key="email_trash:from:linkedin.com")
    store.resolve(first, approve=True, always=True)

    _id2, status2 = store.propose("email_reply", "Reply to LinkedIn", tier=TIER_MEDIUM,
                                  permission_key="email_reply:from:linkedin.com")
    assert status2 == PENDING, "a trash permission leaked into replying"


# ================================================================= §0 P3
def test_high_tier_cannot_be_learned_away(store):
    """Calvin can say 'always yes' to a job application; it still asks next time.

    Applying speaks AS him to an employer. §0 P3 reserves that for a human, and an agent
    that can be talked out of its own safety rule does not have one.
    """
    first, _ = store.propose("job_apply", "Apply to Acme", tier=TIER_HIGH,
                             permission_key="job_apply:company:acme")
    store.resolve(first, approve=True, always=True)
    assert store.decision_for("job_apply:company:acme") == ASK, "a §0 gate was learned away"

    _id2, status2 = store.propose("job_apply", "Apply to Acme again", tier=TIER_HIGH,
                                  permission_key="job_apply:company:acme")
    assert status2 == PENDING


def test_high_tier_denials_ARE_remembered(store):
    """'Never apply to this company' is always safe to honour."""
    first, _ = store.propose("job_apply", "Apply to Shady Corp", tier=TIER_HIGH,
                             permission_key="job_apply:company:shady")
    store.resolve(first, approve=False, always=True)
    assert store.decision_for("job_apply:company:shady") == ALWAYS_DENY
    _id2, status2 = store.propose("job_apply", "Apply to Shady Corp", tier=TIER_HIGH,
                                  permission_key="job_apply:company:shady")
    assert status2 == DENIED


# ================================================================= §0 P4
def test_revoking_a_permission_resets_to_ask_not_delete(store):
    store.remember("email_trash:from:x.com", ALWAYS_APPROVE)
    assert store.forget("email_trash:from:x.com") is True
    assert store.decision_for("email_trash:from:x.com") == ASK
    # the row still exists -- nothing is deleted
    assert any(p["permission_key"] == "email_trash:from:x.com" for p in store.permissions())


def test_high_is_not_in_the_learnable_tiers():
    """The structural guarantee behind test_high_tier_cannot_be_learned_away.

    If `high` ever appears in LEARNABLE_TIERS, a single "always yes" silently authorises the
    agent to apply for jobs as Calvin forever. Asserted on the constant so the protection
    cannot be removed by editing one branch.
    """
    from core.approvals import LEARNABLE_TIERS, TIER_HIGH, TIER_LOW, TIER_MEDIUM

    assert TIER_HIGH not in LEARNABLE_TIERS
    assert set(LEARNABLE_TIERS) == {TIER_LOW, TIER_MEDIUM}


# ================================================================= bulk + listing
def test_pending_lists_only_what_needs_him(store):
    store.propose("email_classify", "trivial", tier=TIER_TRIVIAL, permission_key="a:b:c")
    store.propose("email_trash", "asks", tier=TIER_LOW, permission_key="d:e:f")
    pend = store.pending()
    assert [a.kind for a in pend] == ["email_trash"]


def test_yes_all_resolves_everything_without_learning(store):
    store.propose("email_trash", "one", tier=TIER_LOW, permission_key="k:1")
    store.propose("email_trash", "two", tier=TIER_LOW, permission_key="k:2")
    assert store.resolve_all(approve=True) == 2
    assert store.pending() == []
    # bulk approval is a one-off, not a standing rule
    assert store.decision_for("k:1") == ASK


# ================================================================= reply parsing
@pytest.mark.parametrize("text,expected", [
    ("3 yes", {"bulk": False, "approve": True, "always": False, "id": 3}),
    ("yes 3", {"bulk": False, "approve": True, "always": False, "id": 3}),
    ("3", {"bulk": False, "approve": True, "always": False, "id": 3}),
    ("3 no", {"bulk": False, "approve": False, "always": False, "id": 3}),
    ("always yes 3", {"bulk": False, "approve": True, "always": True, "id": 3}),
    ("always no 12", {"bulk": False, "approve": False, "always": True, "id": 12}),
    ("yes all", {"bulk": True, "approve": True}),
    ("no all", {"bulk": True, "approve": False}),
])
def test_reply_shapes_calvin_would_actually_type(text, expected):
    assert parse_approval_reply(text) == expected


@pytest.mark.parametrize("text", [
    "what's due this week", "start the music", "", "tell me about job 3 later",
])
def test_ordinary_conversation_is_not_swallowed_as_an_approval(text):
    """The approval handler sits in front of normal routing; it must not eat real messages.

    "tell me about job 3 later" contains a number and must still reach the router -- an
    approval parser that grabs any digit would silently approve things he was asking about.
    """
    assert parse_approval_reply(text) is None


# ================================================================= telegram wiring
def test_approval_replies_reach_the_store_from_telegram(mem):
    from core.approvals import ApprovalStore
    from skills.telegram_bot import BotCore

    store = ApprovalStore(memory=mem, clock=lambda: 1_000.0)
    aid, _ = store.propose("email_trash", "Trash LinkedIn promo", tier=TIER_LOW,
                           permission_key="email_trash:from:linkedin.com")

    bot = BotCore(memory=mem)
    out = bot.route_text(f"always yes {aid}")
    assert "Approved" in out and "won't ask" in out
    assert store.decision_for("email_trash:from:linkedin.com") == ALWAYS_APPROVE


def test_a_high_tier_always_yes_says_so_rather_than_silently_ignoring(mem):
    """A safety rule he thinks he switched off, but didn't, is worse than one that says no."""
    from core.approvals import ApprovalStore
    from skills.telegram_bot import BotCore

    store = ApprovalStore(memory=mem, clock=lambda: 1_000.0)
    aid, _ = store.propose("job_apply", "Apply to Acme", tier=TIER_HIGH,
                           permission_key="job_apply:company:acme")
    out = BotCore(memory=mem).route_text(f"always yes {aid}")
    assert "keep asking" in out.lower() or "not remembered" in out.lower()
    assert store.decision_for("job_apply:company:acme") == ASK


def test_normal_messages_still_route_when_actions_are_pending(mem, monkeypatch):
    """A pending action must not turn every message into an approval reply."""
    from core.approvals import ApprovalStore
    from skills.telegram_bot import BotCore

    store = ApprovalStore(memory=mem, clock=lambda: 1_000.0)
    store.propose("email_trash", "Trash something", tier=TIER_LOW, permission_key="k:1")

    bot = BotCore(memory=mem)
    monkeypatch.setattr(bot, "_dispatch", lambda *a, **k: "ROUTED")
    monkeypatch.setattr(bot.registry, "handle_command",
                        lambda t: (type("I", (), {"skill": "x"})(),
                                   type("R", (), {"text": "ROUTED"})()))
    assert bot.route_text("what's due this week") == "ROUTED"


def test_approval_parsing_is_skipped_entirely_when_nothing_is_pending(mem, monkeypatch):
    """With no pending actions, a bare '3' is just conversation."""
    from skills.telegram_bot import BotCore

    bot = BotCore(memory=mem)
    monkeypatch.setattr(bot.registry, "handle_command",
                        lambda t: (type("I", (), {"skill": "chat"})(),
                                   type("R", (), {"text": "ROUTED"})()))
    assert bot.route_text("3") == "ROUTED"
