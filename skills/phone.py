"""Outbound calls + pickup/hangup (Phase 36 Slice 5) — the server half of phone control.

Placing a call is `high` tier: it acts in Calvin's name toward a real person, so it previews
the resolved name and full E.164 number and asks EVERY time — the same two-step
draft-then-confirm shape `skills/email_agent.py`'s `compose`/`continue_send` already uses,
not the ApprovalStore proposal queue (that's for the proactive/background triage case). The
confirmation step is kept OUT of the LLM planner/catalogue entirely
(`kernel/registry.py`'s `_PLAN_INTERNAL_ACTIONS`) — it only ever makes sense as a direct
reply to the preview it followed, never a freestanding action.

Answering or hanging up an ALREADY-ringing/active call is `low` tier (learnable, per §0/
Phase 30): it acts on something already happening, not something initiated in Calvin's name.

This module only ever emits a client action — `{"op": "call", "number": E.164}` /
`{"op": "answer"}` / `{"op": "hangup"}` — for the laptop (client/adb_bridge.py, via
client/hud_window.py's Bridge) to execute against its OWN allowlist and validation. The
number is re-validated here (via skills/contacts.py's E.164 check) before it ever reaches
`data["client_actions"]`, and kernel/app.py's `_client_actions` re-validates it AGAIN before
it reaches the wire — the same "narrow waist, trust nothing from one layer up" pattern
Phase 23 established for desktop app control.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, SkillContract
from skills.contacts import ContactsSkill, normalize_phone
from skills.contacts import SKILL as CONTACTS

log = get_logger("skills.phone")

_CALL_SESSION_KEY = "phone.call_session"
_CONFIRM_PHRASES = {"confirm call", "confirm", "yes call", "call them", "call", "go ahead", "yes"}


class PhoneSkill(BaseSkill):
    name = "phone"

    def __init__(self, memory: Memory | None = None,
                 contacts: ContactsSkill | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._contacts = contacts or CONTACTS
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"call": self.call, "continue_call": self.continue_call,
                "answer": self.answer, "hangup": self.hangup}

    # continue_call only ever makes sense as a direct reply to the preview `call` produced —
    # never a freestanding planned/catalogue-picked action (mirrors email_agent's
    # continue_send, which the planner already excludes the same way).
    plan_exclude = ("continue_call",)

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=[])

    # ------------------------------------------------------------- step 1: preview
    def call(self, name: str = "", **_: Any) -> CommandResult:
        name = (name or "").strip()
        if not name:
            return CommandResult(text="Who should I call?", ok=False)

        found = self._contacts.find(name=name)
        if not found.ok:
            # Ambiguous or zero matches — contacts.find() already asks/declines correctly;
            # dispatch nothing, propagate its answer verbatim rather than re-deciding here.
            return found

        contact = found.data["contact"]
        number = contact.get("phone_e164")
        if not number:
            return CommandResult(
                text=f"I don't have a phone number for {contact['name']}.", ok=False)

        self.mem.kv_set(_CALL_SESSION_KEY, json.dumps({
            "name": contact["name"], "number": number, "created_at": self._now()}))
        return CommandResult(
            text=(f"📞 Call {contact['name']} at {number}?\n"
                  "Say 'confirm call' to place it, or 'cancel'."),
            data={"requires_confirmation": True,
                 "call_preview": {"name": contact["name"], "number": number}})

    # ------------------------------------------------------------- step 2: confirm/cancel
    def continue_call(self, text: str = "", **_: Any) -> CommandResult:
        raw = self.mem.kv_get(_CALL_SESSION_KEY) or ""
        try:
            pending = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            pending = {}
        if not pending:
            return CommandResult(text="No call is waiting to be placed.", ok=False)

        answer = (text or "").strip().lower()
        if answer not in _CONFIRM_PHRASES:
            self.mem.kv_set(_CALL_SESSION_KEY, "")
            return CommandResult(text="Cancelled — no call was placed.")

        number = pending.get("number", "")
        try:
            # Idempotent re-validation, not a re-normalization: the number is already E.164
            # from contacts.py, so this only ever confirms or refuses — it never rewrites it.
            number = normalize_phone(number)
        except ValueError:
            self.mem.kv_set(_CALL_SESSION_KEY, "")
            return CommandResult(text="That number no longer looks valid — not calling.",
                                 ok=False)

        self.mem.kv_set(_CALL_SESSION_KEY, "")
        return CommandResult(
            text=f"Calling {pending.get('name', number)} at {number}.",
            data={"client_actions": [{"op": "call", "number": number}]})

    # ------------------------------------------------------------- pickup / hangup
    def answer(self, **_: Any) -> CommandResult:
        return CommandResult(text="Answering.", data={"client_actions": [{"op": "answer"}]})

    def hangup(self, **_: Any) -> CommandResult:
        return CommandResult(text="Ending the call.", data={"client_actions": [{"op": "hangup"}]})


SKILL = PhoneSkill()
