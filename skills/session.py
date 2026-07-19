"""Session continuity skill (Phase 19).

Exposes the shared, server-side session to every channel: what's live, what's waiting on
Calvin, and — when he moves device — an EXPLICIT confirmation of which thread was picked up.
Ambiguity about "which context am I in?" is the whole failure mode this phase exists to kill,
so the handoff always names the thread, the previous channel, and how long ago.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.logging_setup import get_logger
from core.session import SessionStore
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.session")


class SessionSkill(BaseSkill):
    name = "session"

    def __init__(self, store: SessionStore | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._now = clock

    @property
    def store(self) -> SessionStore:
        if self._store is None:
            self._store = SessionStore(clock=self._now)
        return self._store

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"status": self.status, "handoff": self.handoff, "approvals": self.approvals}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    def contract(self) -> SkillContract:
        """Pure continuity plumbing — no instruction category influences it."""
        return SkillContract(reads_categories=[])

    # ------------------------------------------------------------- status
    def status(self, **_: Any) -> CommandResult:
        s = self.store.get()
        live = s["active_skill"] or self.store.live_skill_session()
        lines = [f"🧵 Session '{s['session_id']}' — one thread, every device."]
        lines.append(f"Last active on: {s['last_channel'] or '(never)'}")
        lines.append(f"Live skill session: {live or 'none'}")
        lines.append(f"Turns remembered: {len(s['turns'])}")
        lines.append(f"Waiting on you: {len(s['pending_approvals'])}")
        current_plan = None
        try:
            current_plan = self.store.mem.current_plan(self.store.session_id)
        except Exception:  # noqa: BLE001 - status stays useful on an older/degraded store
            pass
        lines.append("Current plan: " + (
            f"{current_plan['id']} ({current_plan['status']}) — {current_plan['goal']}"
            if current_plan else "none"))
        if s["turns"]:
            last = s["turns"][-1]
            lines.append(f"Last: “{last['text'][:80]}” → “{last['reply'][:80]}”")
        return CommandResult(text="\n".join(lines),
                             data={"active_skill": live, "last_channel": s["last_channel"],
                                   "turns": len(s["turns"]),
                                   "pending": len(s["pending_approvals"]),
                                   "current_plan": current_plan})

    # ------------------------------------------------------------- handoff
    def handoff(self, channel: str = "", **_: Any) -> CommandResult:
        """'continuing from my phone' -> say exactly which thread was picked up.

        `channel` is the channel he CLAIMS to be coming from; the stored last_channel wins.
        """
        text = self.store.handoff_summary(claimed_from=channel)
        return CommandResult(text=text, data={"handoff": True,
                                              "active_skill": self.store.get()["active_skill"]})

    # ------------------------------------------------------------- approvals
    def approvals(self, **_: Any) -> CommandResult:
        items = self.store.pending_approvals()
        if not items:
            return CommandResult(text="Nothing is waiting on you. 🎉", data={"pending": []})
        lines = [f"⏳ {len(items)} thing(s) waiting on you:"]
        for i in items:
            lines.append(f"  • [{i['kind']} {i['id']}] {i['what']} — {i['action']}")
        return CommandResult(text="\n".join(lines), data={"pending": items})


SKILL = SessionSkill()
