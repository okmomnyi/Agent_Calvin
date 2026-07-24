"""Cross-device session continuity (Phase 19).

AgentOS runs entirely on the VPS, so "access it from any device/OS" isn't a laptop-bound
sync problem — state lives server-side and every channel (Telegram, voice, web dashboard,
CLI) is a thin client into the SAME logical session. Start a thought on the phone, finish
it on the laptop: the agent already has the last N turns, the live skill session, and any
pending approvals.

The session is keyed to Calvin, not to a device. There is exactly one by default.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.session")

DEFAULT_SESSION = "calvin"
MAX_TURNS = 12
CHANNELS = ("telegram", "voice", "dashboard", "cli")

# Live conversational state machines, and where each keeps its state.
_SKILL_SESSIONS = {
    "email_agent.trash_session": "email trash confirmation",
    "cv_tailor.session": "CV refinement",
    "interview_prep.mock": "mock interview",
    "spaced_rep.session": "flashcard quiz",
    "code_tutor.session": "tutor session",
    "phone.call_session": "call confirmation pending",
}


@dataclass
class Turn:
    """One exchange. `skill` is who ROUTED it — distinct from the session's active_skill,
    which is the conversational state machine currently mid-flight (if any)."""

    text: str
    reply: str
    channel: str
    at: float
    skill: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "reply": self.reply, "channel": self.channel,
                "at": self.at, "skill": self.skill}


class SessionStore:
    """Read/write the one logical session. Channel-agnostic by construction."""

    def __init__(self, memory: Memory | None = None,
                 clock: Callable[[], float] = time.time,
                 session_id: str = DEFAULT_SESSION) -> None:
        self._mem = memory
        self._now = clock
        self.session_id = session_id

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    # ------------------------------------------------------------- read
    def get(self) -> dict[str, Any]:
        row = self.mem.execute("SELECT * FROM sessions WHERE session_id=%s",
                               (self.session_id,)).fetchone()
        if not row:
            return {"session_id": self.session_id, "active_skill": None, "turns": [],
                    "last_channel": None, "pending_approvals": [], "updated_at": None}
        return {
            "session_id": row["session_id"],
            "active_skill": row["active_skill"],
            "turns": json.loads(row["context_snapshot"] or "[]"),
            "last_channel": row["last_channel"],
            "pending_approvals": json.loads(row["pending_approvals"] or "[]"),
            "updated_at": row["updated_at"],
        }

    def turns(self, limit: int = MAX_TURNS) -> list[dict[str, Any]]:
        return self.get()["turns"][-limit:]

    # ------------------------------------------------------------- write
    def record_turn(self, text: str, reply: str, channel: str, skill: str | None = None) -> None:
        """Append a turn from ANY channel to the shared session.

        `skill` is recorded on the turn (who handled it). The session's `active_skill`
        always reflects the live conversational state machine instead, so a handoff says
        "you're mid mock-interview", not "the last message hit code_tutor".
        """
        if channel not in CHANNELS:
            log.debug("unknown channel '%s' recorded", channel)
        now = self._now()
        current = self.get()
        turns = current["turns"] + [Turn(text[:2000], reply[:2000], channel, now, skill).to_dict()]
        turns = turns[-MAX_TURNS:]
        approvals = self.pending_approvals()
        with self.mem.tx():
            self.mem.conn.execute(
                "INSERT INTO sessions(session_id, active_skill, context_snapshot, last_channel, "
                "pending_approvals, updated_at) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(session_id) DO UPDATE SET active_skill=excluded.active_skill, "
                "context_snapshot=excluded.context_snapshot, last_channel=excluded.last_channel, "
                "pending_approvals=excluded.pending_approvals, updated_at=excluded.updated_at",
                (self.session_id, self.live_skill_session(), json.dumps(turns), channel,
                 json.dumps(approvals), now))

    # ------------------------------------------------------------- live state
    def live_skill_session(self) -> str | None:
        """Which conversational state machine is mid-flight, if any (kv-backed, so shared)."""
        for key, label in _SKILL_SESSIONS.items():
            if self.mem.kv_get(key):
                return label
        return None

    def pending_approvals(self) -> list[dict[str, Any]]:
        """Everything waiting on Calvin, across every skill — the cross-cutting view.

        Read-only: this never approves anything, it just surfaces what's blocked on him.
        """
        out: list[dict[str, Any]] = []
        q = self.mem.execute
        try:
            for r in q("SELECT id, title, company FROM jobs WHERE status IN ('drafted','notified') "
                       "ORDER BY id LIMIT 20").fetchall():
                out.append({"kind": "job", "id": r["id"],
                            "what": f"{r['title']} @ {r['company']}", "action": "apply/skip"})
            for r in q("SELECT l.id, l.title FROM listings l JOIN pipeline_state p "
                       "ON p.listing_id=l.id WHERE p.state='PURCHASE_GATE' LIMIT 20").fetchall():
                out.append({"kind": "flip", "id": r["id"], "what": r["title"],
                            "action": "confirm availability + approve purchase"})
            for r in q("SELECT id, front FROM flashcards WHERE status='candidate' "
                       "LIMIT 20").fetchall():
                out.append({"kind": "flashcard", "id": r["id"], "what": r["front"],
                            "action": "approve/reject"})
            for r in q("SELECT id, title FROM deadlines WHERE status='pending' LIMIT 20").fetchall():
                out.append({"kind": "deadline", "id": r["id"], "what": r["title"],
                            "action": "confirm/discard"})
            for r in q("SELECT id, skill, signal_type, payload FROM signal_log "
                       "WHERE status='proposed' LIMIT 20").fetchall():
                what = f"{r['signal_type']} ({r['payload']})" if r["payload"] else r["signal_type"]
                out.append({"kind": "rule", "id": r["id"], "what": what,
                            "action": "confirm/reject rule"})
        except Exception:  # noqa: BLE001 - a missing table must not break the session view
            log.exception("collecting pending approvals failed")
        return out

    # ------------------------------------------------------------- handoff
    def handoff_summary(self, claimed_from: str = "") -> str:
        """Explicitly state which thread is being picked up — never silent ambiguity.

        `claimed_from` is the channel Calvin SAYS he's coming from ("continuing from my
        phone"). The session's own last_channel is authoritative; if the two disagree we say
        so out loud rather than quietly resuming a different thread than he expects.
        """
        s = self.get()
        if not s["turns"] and not s["active_skill"]:
            return "Nothing in progress — starting fresh. (No earlier thread to pick up.)"
        parts = []
        if s["last_channel"] and s["updated_at"]:
            mins = max(0, int((self._now() - s["updated_at"]) / 60))
            ago = "just now" if mins < 1 else f"{mins} min ago"
            parts.append(f"Picking up your thread from **{s['last_channel']}** ({ago}).")
            claim = (claimed_from or "").strip().lower()
            aliases = {"phone": "telegram", "browser": "dashboard", "laptop": "voice",
                       "desktop": "voice", "where i left off": s["last_channel"]}
            resolved = aliases.get(claim, claim)
            if resolved and resolved != s["last_channel"]:
                parts.append(f"⚠️ You said “{claimed_from}”, but your last activity was on "
                             f"**{s['last_channel']}** — that's the thread I'm resuming.")
        if s["active_skill"]:
            parts.append(f"Live session: **{s['active_skill']}** — still mid-flight.")
        last = s["turns"][-1] if s["turns"] else None
        if last:
            parts.append(f"Last thing you said: “{last['text'][:120]}”")
            parts.append(f"My last reply: “{last['reply'][:160]}”")
        n = len(s["pending_approvals"])
        if n:
            parts.append(f"{n} thing(s) still waiting on you — say 'approvals' to see them.")
        return "\n".join(parts)
