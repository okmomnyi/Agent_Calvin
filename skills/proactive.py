"""Proactive triage (Phase 32) — the loop that actually acts.

Adapted from OpenJarvis's proactive agent, on top of the Phase 30 tier/permission model.

Until now AgentOS was reactive: it scraped, scored and summarised, then waited for Calvin to
tell it what to do about any of it. His inbox request from a week ago is the proof — he asked
it to clear LinkedIn/Facebook/OKX promotions, it asked him to confirm, and the same mail was
still there days later. Asking is not the same as helping.

The loop, once per run:

  1. Collect what is new since last time, skipping anything already proposed (`seen_ids`),
     so a re-run never asks the same question twice.
  2. Ask the model to classify each item and propose an action with a TIER and a
     PERMISSION KEY (the pattern, e.g. `email_trash:from:linkedin.com`).
  3. Route every proposal through the approval store:
       trivial            -> runs
       learned always-yes -> runs
       learned always-no  -> skipped silently
       everything else    -> queued for him
  4. Execute what was approved.
  5. Send ONE message: what was done, then a numbered list of what needs him. He replies
     "3 yes" or "always no 3" and the pattern is settled for good.

What this deliberately does NOT do:

  * It never deletes. Trash here is Gmail's recoverable trash (§0 P4), and the model is told
    to propose `email_trash` only for mail whose loss would not matter.
  * It never proposes anything that speaks in Calvin's voice. Replying, applying, and sending
    are absent from the action vocabulary entirely -- not merely gated -- because the safest
    version of "the agent emailed someone overnight" is that it could not have.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from core.approvals import (APPROVED, TIER_HIGH, TIER_LOW, TIER_MEDIUM, TIER_TRIVIAL,
                            ApprovalStore, get_store)
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.proactive")

# The ONLY things it may propose. Every one is reversible and none speaks to another human.
# `email_reply`, `job_apply` and anything else that acts in Calvin's name are absent on
# purpose: an action the vocabulary cannot express is one an overnight run cannot take.
ACTION_KINDS = {
    "email_archive": TIER_LOW,     # out of the inbox, still in All Mail
    "email_trash":   TIER_LOW,     # Gmail trash: recoverable for 30 days
    "email_label":   TIER_TRIVIAL,  # categorisation only
}

# Labels are a closed vocabulary for the same reason the action kinds are: an LLM free to
# invent label names would slowly shard the mailbox into near-duplicates ("Promos", "Promo",
# "Promotions"), and every one of them is a folder Calvin then has to learn. These are the
# categories `email_agent` already files under, so both paths land in the same AgentOS/* tree.
LABEL_CATEGORIES = ("promotion", "newsletter", "social", "job_related", "important", "personal")
_DEFAULT_LABEL = "promotion"

_SCHEMA = ('{"actions": [{"kind": string, "description": string, "message_id": string, '
           '"permission_key": string, "tier": string, "label": string, "reasoning": string}]}')

_SYSTEM = """\
You triage a personal inbox. For each message decide whether it can be filed away without \
the owner reading it, and propose at most ONE action for it.

Allowed kinds ONLY:
  email_label   — categorise, nothing else (tier: trivial)
  email_archive — out of the inbox, still findable (tier: low)
  email_trash   — recoverable trash, for mail whose loss would not matter (tier: low)

NEVER propose replying, sending, applying, or anything that contacts another person.

Propose an action ONLY for clear bulk mail: marketing, newsletters, social notifications, \
promotions, automated receipts already seen. If a message could plausibly need a human — \
anything from a person, a recruiter, a university, a bank, an invoice, a deadline, a \
security alert — propose NOTHING for it. When unsure, propose nothing: a missed newsletter \
costs nothing, a missed interview invite costs a job.

For email_label, also set `label` to exactly one of: promotion, newsletter, social, \
job_related, important, personal. Any other value is discarded.

permission_key is the PATTERN, formatted `<kind>:from:<sender-domain>`, e.g. \
`email_trash:from:linkedin.com`. Getting this right matters: it is what lets the owner say \
"always yes" once instead of every week.

Keep description and reasoning to one short sentence each. At most 12 actions. \
Output ONLY the JSON object."""


class ProactiveSkill(BaseSkill):
    name = "proactive"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 gmail: Any | None = None, email_agent: Any | None = None,
                 store: ApprovalStore | None = None,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._mem = memory
        self._llm = llm
        self._gmail = gmail
        self._email = email_agent
        self._store = store
        self._notify = notify or send_telegram

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def store(self) -> ApprovalStore:
        if self._store is None:
            self._store = get_store(self._mem)
        return self._store

    @property
    def email(self):
        if self._email is None:
            from skills.email_agent import SKILL as email_skill

            self._email = email_skill
        return self._email

    @property
    def gmail(self):
        if self._gmail is None:
            self._gmail = self.email.gmail
        return self._gmail

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"triage": self.triage, "pending": self.pending, "permissions": self.permissions,
                "forget": self.forget_permission}

    def contract(self) -> SkillContract:
        return SkillContract(
            reads_categories=["notifications", "general"],
            hard_invariants=["never_contacts_anyone", "reversible_actions_only"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        # 05:30, before the 07:00 briefing -- so the briefing reports an inbox that has
        # already been triaged rather than one still full of noise.
        return [ScheduledJob(id="proactive.triage", func=self.triage, trigger="cron",
                             kwargs={"hour": 5, "minute": 30},
                             queued=True, skill="proactive", action="triage")]

    # ------------------------------------------------------------- the loop
    def triage(self, max_messages: int = 40, notify: bool = True, **_: Any) -> CommandResult:
        expired = self.store.expire_stale()
        try:
            candidates = self._collect(max_messages)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Couldn't read the inbox: {exc}", ok=False)
        if not candidates:
            return CommandResult(text="Nothing new to triage.",
                                 data={"proposed": 0, "done": 0, "expired": expired})

        try:
            proposals = self._classify(candidates)
        except LLMError as exc:
            return CommandResult(text=f"Couldn't classify the inbox right now: {exc}", ok=False)

        done, pending = self._apply(proposals, candidates)
        text = self._summary(done, pending, expired)
        if notify and (done or pending):
            self._notify(text)
        return CommandResult(text=text,
                             data={"proposed": len(proposals), "done": len(done),
                                   "pending": len(pending), "expired": expired})

    def _collect(self, limit: int) -> list[dict[str, str]]:
        """Inbox metadata for messages not already proposed on."""
        seen = self.store.seen_ids("email_")
        out: list[dict[str, str]] = []
        for msg_id in self.gmail.list_inbox(max_results=limit):
            if msg_id in seen:
                continue
            msg = self.gmail.get_message(msg_id, fmt="metadata")
            out.append({
                "id": msg_id,
                "sender": self.gmail.header(msg, "From") or "",
                "subject": self.gmail.header(msg, "Subject") or "",
                "snippet": (msg.get("snippet") or "")[:180],
            })
        return out

    def _classify(self, items: list[dict[str, str]]) -> list[dict[str, Any]]:
        listing = "\n".join(
            f"[{i['id']}] From: {i['sender']} | Subject: {i['subject']} | {i['snippet']}"
            for i in items)
        data = self.llm.chat_json(
            "classify",
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": f"Messages:\n{listing}"}],
            schema_hint=_SCHEMA, temperature=0.0, max_tokens=1600)
        return [a for a in (data.get("actions") or []) if isinstance(a, dict)]

    def _apply(self, proposals: list[dict[str, Any]],
               items: list[dict[str, str]]) -> tuple[list[str], list[Any]]:
        by_id = {i["id"]: i for i in items}
        done: list[str] = []
        for p in proposals:
            kind = str(p.get("kind", ""))
            msg_id = str(p.get("message_id", ""))
            if kind not in ACTION_KINDS:
                # Dropping an out-of-vocabulary proposal is the point: the model cannot widen
                # its own authority by inventing a verb.
                log.info("proactive: dropped out-of-vocabulary proposal %r", kind)
                continue
            if msg_id not in by_id:
                # A different failure entirely -- the model referenced a message that wasn't
                # in this batch (hallucinated id, or one already triaged). Logged distinctly,
                # because reporting it as "out-of-vocabulary" sent me chasing the wrong bug.
                log.info("proactive: proposal referenced unknown message %r", msg_id)
                continue
            # The TIER comes from our table, never from the model. Otherwise "tier": "trivial"
            # in a generated payload would be enough to auto-run anything it liked.
            tier = ACTION_KINDS[kind]
            key = str(p.get("permission_key") or f"{kind}:from:{by_id[msg_id]['sender']}")
            # Same rule as the tier: the label is validated against our own vocabulary rather
            # than trusted from the payload, so a generated value can only ever pick one of
            # ours or fall back to the default.
            label = str(p.get("label") or "").strip().lower()
            if label not in LABEL_CATEGORIES:
                label = _DEFAULT_LABEL
            # Built from the message's own metadata, not trusted from the model's freeform
            # `description` field: the model gave every LinkedIn notification in a batch the
            # SAME description ("Bulk job mail"), which made ten pending rows read as
            # identical text with no way to tell them apart. Sender + subject is always
            # distinct per real message (or, when genuinely identical, correctly reads as
            # the same thing rather than by coincidence of what the model happened to write).
            sender = by_id[msg_id]["sender"]
            subject = by_id[msg_id]["subject"][:60] or "(no subject)"
            description = f"{sender} — \"{subject}\""
            action_id, status = self.store.propose(
                kind, description, tier=tier, permission_key=key,
                payload={"message_id": msg_id, "doc_id": msg_id, "label": label,
                        "sender": sender, "subject": subject},
                reasoning=str(p.get("reasoning") or "")[:200])
            if status == APPROVED:
                if self._execute(action_id, kind, msg_id, label):
                    done.append(f"{kind.replace('email_', '')}: {by_id[msg_id]['subject'][:60]}")
        return done, self.store.pending()

    def _execute(self, action_id: int, kind: str, msg_id: str,
                 label: str = _DEFAULT_LABEL) -> bool:
        try:
            if kind == "email_trash":
                self.gmail.trash(msg_id)
            elif kind == "email_archive":
                # Archive under the category label too, so archived mail is findable by the
                # same AgentOS/* folder the labeller uses rather than only in All Mail.
                self.gmail.archive(msg_id, self.gmail.category_label(label))
            else:
                # email_label: categorise in place -- the message stays in the inbox, it just
                # gains AgentOS/<Category>. This branch used to be a bare `pass`, so every
                # trivial-tier action was counted, reported to Calvin, and marked executed
                # while doing nothing at all. A no-op that reports success is worse than an
                # unimplemented action, because nobody goes looking for it.
                self.gmail.add_label(msg_id, self.gmail.category_label(label))
            self.store.mark(action_id, "executed")
            return True
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the run
            log.exception("proactive: %s failed for %s", kind, msg_id)
            self.store.mark(action_id, "failed", str(exc))
            return False

    def _summary(self, done: list[str], pending: list[Any], expired: int) -> str:
        lines = ["🧹 Inbox triage"]
        if done:
            lines.append(f"\nHandled {len(done)} automatically:")
            lines += [f"  • {d}" for d in done[:8]]
            if len(done) > 8:
                lines.append(f"  … and {len(done) - 8} more")
        if pending:
            lines.append(f"\nNeeds you ({len(pending)}):")
            # Grouped by sender (each row's description is now "sender — subject", see
            # _apply()) so ten LinkedIn notifications collapse into one readable line with a
            # count, instead of ten identical-looking rows. The header said 17 while only 10
            # rows ever printed, with no indication the list was cut short -- Calvin could
            # never answer what he couldn't see, so the same 17 got re-presented daily until
            # they expired. `showing N of M` makes the truncation honest.
            groups: dict[str, list[Any]] = {}
            for a in pending:
                sender = (a.payload or {}).get("sender") or "unknown sender"
                groups.setdefault(sender, []).append(a)
            max_rows = 10
            shown_actions = 0
            for sender, items in groups.items():
                if len(lines) - 1 >= max_rows:  # rows appended below this point so far
                    break
                first = items[0]
                subject = (first.payload or {}).get("subject", first.description)
                suffix = f" (+{len(items) - 1} similar)" if len(items) > 1 else ""
                lines.append(f"  [{first.id}] {sender} — {subject}{suffix}")
                shown_actions += len(items)
            if shown_actions < len(pending):
                lines.append(f"\n(showing {shown_actions} of {len(pending)})")
            lines.append("\nReply `3 yes`, `3 no`, or `always yes 3` to settle the pattern.")
        if not done and not pending:
            lines.append("\nNothing needed doing.")
        if expired:
            lines.append(f"\n({expired} old request(s) expired unanswered.)")
        return "\n".join(lines)

    # ------------------------------------------------------------- inspection
    def pending(self, **_: Any) -> CommandResult:
        items = self.store.pending()
        if not items:
            return CommandResult(text="Nothing waiting on you.", data={"count": 0})
        lines = [f"⏳ {len(items)} action(s) waiting:"]
        lines += [f"  [{a.id}] ({a.tier}) {a.description}" for a in items]
        lines.append("\nReply `3 yes` / `3 no` / `always yes 3`.")
        return CommandResult(text="\n".join(lines), data={"count": len(items)})

    def permissions(self, **_: Any) -> CommandResult:
        rows = [p for p in self.store.permissions() if p["decision"] != "ask"]
        if not rows:
            return CommandResult(text="No standing permissions yet — I ask about everything.",
                                 data={"count": 0})
        lines = ["🔑 What I no longer ask about:"]
        for p in rows[:25]:
            verb = "always" if p["decision"] == "always_approve" else "never"
            lines.append(f"  {verb}: {p['permission_key']}")
        lines.append("\n`/forget <pattern>` to make me ask again.")
        return CommandResult(text="\n".join(lines), data={"count": len(rows)})

    def forget_permission(self, pattern: str = "", **_: Any) -> CommandResult:
        if not pattern:
            return CommandResult(text="Which pattern? e.g. `forget email_trash:from:linkedin.com`",
                                 ok=False)
        return CommandResult(
            text=(f"I'll ask about `{pattern}` again." if self.store.forget(pattern)
                  else f"No standing permission for `{pattern}`."))


SKILL = ProactiveSkill()
