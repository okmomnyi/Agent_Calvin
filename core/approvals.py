"""Tiered actions with learned permissions (Phase 30).

Adapted from OpenJarvis's proactive-agent model, kept inside AgentOS's §0 rules.

The problem it solves: AgentOS's approval gate was all-or-nothing. Every action waited for
Calvin, and `AUTO_APPLY` was a single global switch. There was no way to express "trashing
LinkedIn marketing is routine, emailing a recruiter is not", and no way to LEARN that he
always says yes to the first and always wants asking about the second. So he approved the
same class of thing forever, which is why his inbox triage never actually got done.

Two ideas, both from OpenJarvis:

* **Tier** — how consequential an action is:
      trivial  read-only / categorisation, no external effect      -> just do it
      low      reversible and routine (archive, trash a newsletter) -> ask once per pattern
      medium   affects someone else but is expected                 -> ask once per pattern
      high     speaks in Calvin's voice, or is irreversible         -> ALWAYS ask
* **Permission key** — the PATTERN an action belongs to, e.g.
  `email_trash:from:linkedin.com`. A decision is remembered against the key, not the
  individual action, so "always yes" generalises to every future message from that sender.

Where this deliberately diverges from OpenJarvis:

* `high` can never be auto-approved, even if Calvin says "always yes". Applying to a job or
  emailing a stranger speaks AS him (§0 P3); that gate does not get to be learned away.
* There is no `delete`. OpenJarvis auto-deletes at low tier; AgentOS trashes recoverably and
  never permanently removes anything (§0 P4). Same UX, no data loss.
* A remembered decision is never silently applied to a DIFFERENT action type: the key
  includes the action type, so "always trash LinkedIn" cannot become "always reply to
  LinkedIn".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.approvals")

TIER_TRIVIAL, TIER_LOW, TIER_MEDIUM, TIER_HIGH = "trivial", "low", "medium", "high"
TIERS = (TIER_TRIVIAL, TIER_LOW, TIER_MEDIUM, TIER_HIGH)

ALWAYS_APPROVE, ALWAYS_DENY, ASK = "always_approve", "always_deny", "ask"

# Tiers that a learned "always yes" may auto-run. `high` is absent on purpose and must stay
# that way: it is the tier for acting in Calvin's name, which §0 P3 reserves for him.
LEARNABLE_TIERS = (TIER_LOW, TIER_MEDIUM)

PENDING, APPROVED, DENIED, EXECUTED, FAILED = (
    "pending", "approved", "denied", "executed", "failed")


@dataclass
class Action:
    id: int
    kind: str                 # e.g. "email_trash"
    description: str
    payload: dict[str, Any]
    tier: str
    permission_key: str
    status: str
    reasoning: str = ""


class ApprovalStore:
    """Proposed actions + the permissions Calvin has taught it."""

    def __init__(self, memory: Memory | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    # ------------------------------------------------------------- permissions
    def decision_for(self, permission_key: str) -> str:
        row = self.mem.execute(
            "SELECT decision FROM action_permissions WHERE permission_key=%s",
            (permission_key,)).fetchone()
        return row["decision"] if row else ASK

    def remember(self, permission_key: str, decision: str) -> None:
        """Teach it a standing answer for this pattern."""
        if decision not in (ALWAYS_APPROVE, ALWAYS_DENY, ASK):
            raise ValueError(f"unknown decision {decision!r}")
        with self.mem.tx() as conn:
            conn.execute(
                "INSERT INTO action_permissions(permission_key, decision, updated_at) "
                "VALUES(%s,%s,%s) ON CONFLICT(permission_key) DO UPDATE SET "
                "decision=EXCLUDED.decision, updated_at=EXCLUDED.updated_at",
                (permission_key, decision, self._now()))

    def permissions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.mem.execute(
            "SELECT permission_key, decision, updated_at FROM action_permissions "
            "ORDER BY updated_at DESC").fetchall()]

    def forget(self, permission_key: str) -> bool:
        """Revert a pattern to asking. Never deletes history -- sets it back to ASK (§0 P4)."""
        if self.decision_for(permission_key) == ASK:
            return False
        self.remember(permission_key, ASK)
        return True

    # ------------------------------------------------------------- proposing
    def propose(self, kind: str, description: str, *, tier: str, permission_key: str,
                payload: dict[str, Any] | None = None, reasoning: str = "") -> tuple[int, str]:
        """Record a proposed action and decide, right now, whether it may run.

        Returns (action_id, status). The caller executes only when status == APPROVED.
        """
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        learned = self.decision_for(permission_key)

        if tier == TIER_TRIVIAL:
            status = APPROVED                     # no external effect; nothing to gate
        elif learned == ALWAYS_DENY:
            status = DENIED
        elif learned == ALWAYS_APPROVE and tier in LEARNABLE_TIERS:
            status = APPROVED
        else:
            # Everything else -- including every `high` action, even one he said "always yes"
            # to -- waits for him.
            status = PENDING

        with self.mem.tx() as conn:
            row = conn.execute(
                "INSERT INTO pending_actions(kind, description, payload, tier, permission_key, "
                "status, reasoning, created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (kind, description, json.dumps(payload or {}), tier, permission_key,
                 status, reasoning, self._now())).fetchone()
        return int(row["id"]), status

    # ------------------------------------------------------------- reading
    def pending(self, limit: int = 20) -> list[Action]:
        rows = self.mem.execute(
            "SELECT * FROM pending_actions WHERE status=%s ORDER BY id LIMIT %s",
            (PENDING, limit)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, action_id: int) -> Action | None:
        row = self.mem.execute("SELECT * FROM pending_actions WHERE id=%s",
                               (action_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(r: Any) -> Action:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        return Action(id=int(r["id"]), kind=r["kind"], description=r["description"],
                      payload=payload or {}, tier=r["tier"],
                      permission_key=r["permission_key"], status=r["status"],
                      reasoning=r["reasoning"] or "")

    # ------------------------------------------------------------- resolving
    def resolve(self, action_id: int, approve: bool, *, always: bool = False) -> Action | None:
        """Approve/deny one action; `always` also teaches the pattern for next time."""
        action = self.get(action_id)
        if action is None:
            return None
        status = APPROVED if approve else DENIED
        with self.mem.tx() as conn:
            conn.execute("UPDATE pending_actions SET status=%s, resolved_at=%s WHERE id=%s",
                         (status, self._now(), action_id))
        if always:
            if action.tier == TIER_HIGH and approve:
                # Refuse to learn away a §0 P3 gate. Denials CAN be remembered -- "never do
                # this again" is always safe to honour.
                log.info("not learning always-approve for high-tier %s", action.permission_key)
            else:
                self.remember(action.permission_key,
                              ALWAYS_APPROVE if approve else ALWAYS_DENY)
        action.status = status
        return action

    def resolve_all(self, approve: bool) -> int:
        """'yes all' / 'no all' — bulk-resolve everything pending, learning nothing."""
        pend = self.pending(limit=200)
        for a in pend:
            self.resolve(a.id, approve)
        return len(pend)

    def mark(self, action_id: int, status: str, error: str = "") -> None:
        """Record the outcome after execution (executed | failed). Rows are never deleted."""
        with self.mem.tx() as conn:
            conn.execute("UPDATE pending_actions SET status=%s, error=%s, resolved_at=%s "
                         "WHERE id=%s", (status, error[:500], self._now(), action_id))

    def seen_ids(self, kind_prefix: str = "") -> set[str]:
        """Source ids already proposed, so a re-run never asks about the same thing twice.

        Idempotency is what makes a scheduled triage safe to run hourly: without it, every
        pass re-proposes the same fifty emails and the summary becomes noise Calvin ignores.
        """
        sql = "SELECT payload FROM pending_actions"
        params: tuple[Any, ...] = ()
        if kind_prefix:
            sql += " WHERE kind LIKE %s"
            params = (f"{kind_prefix}%",)
        out: set[str] = set()
        for r in self.mem.execute(sql, params).fetchall():
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload or "{}")
                except json.JSONDecodeError:
                    continue
            for key in ("doc_id", "message_id", "id"):
                if payload and payload.get(key):
                    out.add(str(payload[key]))
        return out

    def expire_stale(self, older_than_hours: float = 72.0) -> int:
        """Retire pending actions Calvin never answered.

        A three-day-old "shall I archive this?" is no longer a useful question, and a list
        that only grows is a list nobody reads. Expired rows are kept (§0 P4) -- they just
        stop appearing in `pending()`.
        """
        cutoff = self._now() - older_than_hours * 3600
        with self.mem.tx() as conn:
            rows = conn.execute(
                "UPDATE pending_actions SET status='expired', resolved_at=%s "
                "WHERE status=%s AND created_at < %s RETURNING id",
                (self._now(), PENDING, cutoff)).fetchall()
        return len(rows)

    def approved(self, limit: int = 50) -> list[Action]:
        rows = self.mem.execute(
            "SELECT * FROM pending_actions WHERE status=%s ORDER BY id LIMIT %s",
            (APPROVED, limit)).fetchall()
        return [self._row(r) for r in rows]


# ------------------------------------------------------------------ reply parsing
def parse_approval_reply(text: str) -> dict[str, Any] | None:
    """Understand Calvin's reply to an approval summary.

    Accepts the shapes OpenJarvis uses, because they are the ones that read naturally in a
    chat window:
        "3 yes" / "yes 3" / "3"        approve one
        "3 no" / "no 3"                deny one
        "always yes 3" / "3 always yes"  approve + remember the pattern
        "always no 3"                  deny + remember
        "yes all" / "approve all"      bulk approve
        "no all" / "deny all"          bulk deny
    Returns None when the text isn't an approval reply at all, so ordinary conversation is
    never swallowed by the approval handler.
    """
    import re

    t = (text or "").strip().lower()
    if not t:
        return None

    if re.fullmatch(r"(?:yes|approve|ok)\s+all", t):
        return {"bulk": True, "approve": True}
    if re.fullmatch(r"(?:no|deny|reject)\s+all", t):
        return {"bulk": True, "approve": False}

    always = bool(re.search(r"\balways\b", t))
    ids = re.findall(r"\b(\d{1,6})\b", t)
    if not ids:
        return None
    action_id = int(ids[0])

    if re.search(r"\b(?:no|deny|reject|skip)\b", t):
        approve = False
    elif re.search(r"\b(?:yes|approve|ok|do it)\b", t):
        approve = True
    elif re.fullmatch(r"\d{1,6}", t):
        approve = True                    # a bare number means "yes, that one"
    else:
        return None
    return {"bulk": False, "approve": approve, "always": always, "id": action_id}


def get_store(memory: Memory | None = None) -> ApprovalStore:
    return ApprovalStore(memory=memory)
