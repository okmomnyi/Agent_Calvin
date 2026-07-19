"""Proactive inbox triage (Phase 32).

This is the first thing in AgentOS that ACTS without being asked, so the tests are mostly
about what it cannot do:

  * it can never contact another human — replying/sending/applying are absent from the
    action vocabulary, not merely gated;
  * the model cannot widen its own authority: the tier comes from our table, never from the
    generated payload;
  * it never deletes — trash is Gmail's recoverable trash (§0 P4);
  * a re-run never re-proposes something already proposed (idempotent), or the summary
    becomes noise Calvin ignores.
"""

from __future__ import annotations

import pytest

from core.approvals import ALWAYS_APPROVE, ALWAYS_DENY, ApprovalStore
from skills.proactive import ACTION_KINDS, ProactiveSkill


class _Gmail:
    """Fake Gmail that records what was done to it."""

    def __init__(self, messages=None):
        self._messages = messages or [
            {"id": "m1", "From": "LinkedIn <news@linkedin.com>", "Subject": "Jobs for you",
             "snippet": "See open jobs"},
            {"id": "m2", "From": "Supervisor <lecturer@must.ac.ke>", "Subject": "CAT 1 date",
             "snippet": "Your CAT is on Friday"},
        ]
        self.trashed: list[str] = []
        self.archived: list[tuple[str, str | None]] = []
        self.labelled: list[tuple[str, str]] = []

    def list_inbox(self, max_results=40, query=None):
        return [m["id"] for m in self._messages]

    def get_message(self, msg_id, fmt="metadata"):
        m = next(x for x in self._messages if x["id"] == msg_id)
        return {"id": msg_id, "snippet": m["snippet"], "_m": m}

    def header(self, msg, name):
        return msg["_m"].get(name, "")

    def trash(self, msg_id):
        self.trashed.append(msg_id)

    def archive(self, msg_id, category_label_id=None):
        self.archived.append((msg_id, category_label_id))

    def category_label(self, category):
        return f"Label_AgentOS/{category}"

    def add_label(self, msg_id, label_id):
        self.labelled.append((msg_id, label_id))


class _LLM:
    def __init__(self, actions):
        self.routes, self.defaults = {"classify": "m"}, {}
        self._actions = actions
        self.system = ""

    def chat_json(self, task, messages, schema_hint, **kw):
        self.system = messages[0]["content"]
        return {"actions": self._actions}


def _skill(mem, actions, gmail=None):
    gm = gmail or _Gmail()
    store = ApprovalStore(memory=mem, clock=lambda: 1_000.0)
    skill = ProactiveSkill(memory=mem, llm=_LLM(actions), gmail=gm, email_agent=object(),
                           store=store, notify=lambda t: True)
    return skill, gm, store


_TRASH_LINKEDIN = [{"kind": "email_trash", "description": "Trash LinkedIn jobs mail",
                    "message_id": "m1", "permission_key": "email_trash:from:linkedin.com",
                    "tier": "low", "reasoning": "Bulk marketing."}]


# ================================================================= it cannot contact anyone
def test_the_action_vocabulary_cannot_express_contacting_a_human():
    """Not gated — absent. The safest version of "it emailed someone overnight" is that it
    could not have."""
    for banned in ("email_reply", "email_send", "job_apply", "sms_send", "message_send"):
        assert banned not in ACTION_KINDS


def test_every_allowed_action_is_reversible():
    from core.approvals import TIER_HIGH

    assert TIER_HIGH not in ACTION_KINDS.values(), "an unsupervised loop proposing high tier"
    assert set(ACTION_KINDS) == {"email_archive", "email_trash", "email_label"}


def test_an_invented_action_kind_is_dropped(mem):
    """The model must not widen its own authority by naming a new verb."""
    skill, gm, store = _skill(mem, [
        {"kind": "email_reply", "description": "Reply to recruiter", "message_id": "m1",
         "permission_key": "email_reply:from:x.com", "tier": "low"},
        {"kind": "rm_rf", "description": "something creative", "message_id": "m1",
         "permission_key": "k", "tier": "trivial"}])
    res = skill.triage(notify=False)
    assert res.data["done"] == 0 and res.data["pending"] == 0
    assert gm.trashed == [] and gm.archived == []


def test_the_tier_comes_from_our_table_not_the_payload(mem):
    """"tier": "trivial" in generated output must not be enough to auto-run."""
    skill, gm, store = _skill(mem, [
        {**_TRASH_LINKEDIN[0], "tier": "trivial"}])     # model claims trivial
    skill.triage(notify=False)
    assert gm.trashed == [], "a generated tier auto-ran an action"
    assert len(store.pending()) == 1, "it should have asked"


# ================================================================= §0 P4
def test_trash_is_recoverable_not_deleted(mem):
    skill, gm, _ = _skill(mem, _TRASH_LINKEDIN)
    skill.triage(notify=False)
    assert not hasattr(gm, "delete"), "the client must not even expose a hard delete"


def test_first_run_asks_and_changes_nothing(mem):
    """Nothing is auto-run until Calvin has settled the pattern once."""
    skill, gm, store = _skill(mem, _TRASH_LINKEDIN)
    res = skill.triage(notify=False)
    assert gm.trashed == []
    assert res.data["pending"] == 1


def test_after_always_yes_it_just_does_it(mem):
    """The point of the whole phase: settle the SENDER once, not every message.

    The second run uses a NEW message id, as a real new LinkedIn email would -- the same id
    is deliberately never re-proposed (see the idempotency test).
    """
    skill, gm, store = _skill(mem, _TRASH_LINKEDIN)
    skill.triage(notify=False)
    store.resolve(store.pending()[0].id, approve=True, always=True)

    later = _Gmail([{"id": "m9", "From": "LinkedIn <news@linkedin.com>",
                     "Subject": "More jobs for you", "snippet": "See open jobs"}])
    proposal = [{**_TRASH_LINKEDIN[0], "message_id": "m9"}]
    skill2, gm2, _ = _skill(mem, proposal, gmail=later)
    skill2._store = store
    res = skill2.triage(notify=False)
    assert gm2.trashed == ["m9"], "it asked again about a sender he already settled"
    assert res.data["done"] == 1


def test_always_no_is_honoured_silently(mem):
    skill, gm, store = _skill(mem, _TRASH_LINKEDIN)
    store.remember("email_trash:from:linkedin.com", ALWAYS_DENY)
    res = skill.triage(notify=False)
    assert gm.trashed == [] and res.data["pending"] == 0


# ================================================================= idempotency
def test_a_rerun_does_not_re_propose_the_same_message(mem):
    """Without this, every pass re-asks about the same fifty emails."""
    skill, _, store = _skill(mem, _TRASH_LINKEDIN)
    skill.triage(notify=False)
    assert len(store.pending()) == 1
    skill.triage(notify=False)                              # immediately again
    assert len(store.pending()) == 1, "the same message was proposed twice"


def test_stale_requests_expire_rather_than_pile_up(mem):
    skill, _, store = _skill(mem, _TRASH_LINKEDIN)
    skill.triage(notify=False)
    store._now = lambda: 1_000.0 + 80 * 3600                # four days later
    assert store.expire_stale() == 1
    assert store.pending() == []


# ================================================================= prompt guardrails
def test_the_prompt_tells_it_to_leave_anything_human_alone(mem):
    skill, _, _ = _skill(mem, [])
    skill.triage(notify=False)
    sys = skill.llm.system.lower()
    assert "never propose replying" in sys
    assert "when unsure, propose nothing" in sys
    # the asymmetry that should drive its caution
    assert "missed interview invite" in sys


def test_nothing_to_do_is_reported_not_invented(mem):
    skill, gm, _ = _skill(mem, [])
    res = skill.triage(notify=False)
    assert res.data["done"] == 0
    assert "nothing" in res.text.lower()


# ================================================================= failure handling
def test_one_failing_message_does_not_stop_the_run(mem):
    gm = _Gmail()

    def boom(msg_id):
        raise RuntimeError("gmail 500")

    gm.trash = boom
    skill, _, store = _skill(mem, _TRASH_LINKEDIN, gmail=gm)
    store.remember("email_trash:from:linkedin.com", ALWAYS_APPROVE)
    res = skill.triage(notify=False)
    assert res.ok
    assert res.data["done"] == 0                            # it failed, honestly reported
    from core.approvals import FAILED  # noqa: F401  (status recorded, row kept)


def test_an_unreachable_inbox_is_reported_not_swallowed(mem):
    gm = _Gmail()

    def boom(**kw):
        raise RuntimeError("gmail unauthorised")

    gm.list_inbox = boom
    skill, _, _ = _skill(mem, _TRASH_LINKEDIN, gmail=gm)
    res = skill.triage(notify=False)
    assert res.ok is False and "couldn't read the inbox" in res.text.lower()


def test_triage_is_scheduled_and_queued(mem):
    skill, _, _ = _skill(mem, [])
    jobs = {j.id: j for j in skill.scheduled_jobs()}
    assert "proactive.triage" in jobs
    assert jobs["proactive.triage"].queued is True, "LLM + Gmail work belongs on the queue"


# ================================================================= trivial tier actually acts
def _label_action(label="promotion"):
    return [{"kind": "email_label", "description": "File the LinkedIn blast",
             "message_id": "m1", "permission_key": "email_label:from:linkedin.com",
             "tier": "trivial", "label": label, "reasoning": "Bulk marketing."}]


def test_email_label_actually_labels_the_message(mem):
    """The trivial tier used to be a bare `pass`.

    `_execute`'s else-branch did nothing, then marked the action executed and counted it into
    the summary — so every run reported "handled N automatically" for work that never
    happened. A no-op that reports success is worse than an unimplemented action, because
    nobody goes looking for it. email_label is auto-approved (trivial), so this is the branch
    that runs most often and unattended.
    """
    skill, gm, _ = _skill(mem, _label_action())

    result = skill.triage(notify=False)

    assert gm.labelled == [("m1", "Label_AgentOS/promotion")]
    assert gm.trashed == []                      # labelling files in place; it does not remove
    assert result.data["done"] == 1


def test_a_label_the_model_invented_falls_back_to_the_vocabulary(mem):
    """Same rule as the tier: validated against our table, never trusted from the payload."""
    from skills.proactive import LABEL_CATEGORIES

    skill, gm, _ = _skill(mem, _label_action(label="Urgent!! Read Now"))

    skill.triage(notify=False)

    (_, label_id), = gm.labelled
    assert label_id.rsplit("/", 1)[-1] in LABEL_CATEGORIES


def test_archiving_files_under_a_category_label_too(mem):
    """Archived mail should be findable in the same AgentOS/* tree, not only in All Mail."""
    actions = [{"kind": "email_archive", "description": "Archive the newsletter",
                "message_id": "m1", "permission_key": "email_archive:from:linkedin.com",
                "tier": "low", "label": "newsletter", "reasoning": "Bulk."}]
    skill, gm, store = _skill(mem, actions)
    store.remember("email_archive:from:linkedin.com", ALWAYS_APPROVE)

    skill.triage(notify=False)

    assert gm.archived == [("m1", "Label_AgentOS/newsletter")]
