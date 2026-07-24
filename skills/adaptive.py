"""Adaptive behavior layer + weekly retro (Phase 20).

The mechanism behind "morphs around me without touching code", extended so the agent can
NOTICE patterns rather than only waiting for Calvin to state rules.

How it stays safe:
  * **Passive logging** — skills log lightweight signals (an event skipped, a job skipped).
    Nothing is acted on at log time.
  * **Confidence threshold** — a pattern must repeat (default 4x) with NO contradicting
    instance before it's even a candidate. Consistency, not a majority vote — one
    counter-example is enough to disqualify it, which avoids overfitting to a one-off.
  * **Calvin decides** — a candidate is only ever a proposal (Confirm / Reject / Not now).
    Confirmed rules land in the same Standing Instructions store every skill already reads.
    Rejected patterns are recorded as "seen, declined" and never proposed again.
  * **Skill Contract boundary check** — a rule is refused unless some skill declares it
    reads that category, and can never contradict a hard invariant (§0 Principles 3,4,5,8,9).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.persona_store import get_engine
from core.skill import (INSTRUCTION_CATEGORIES, UNIVERSAL_INVARIANTS, BaseSkill,
                        CommandResult, ScheduledJob, SkillContract)

log = get_logger("skills.adaptive")

DEFAULT_THRESHOLD = 4

# Set while a retro prompt is awaiting his reply; kernel/registry.py's _active_continuation
# routes the next free-text message here instead of the general router. Without this (#22)
# an answer to "what worked this week" had nowhere to land -- it just fell through to
# whatever the keyword/LLM router guessed, and was silently lost either way.
_RETRO_SESSION_KEY = "adaptive.retro_session"

# Phrases that would switch off a §0 invariant. A proposed rule matching one is refused
# outright — no instruction, however often the pattern repeats, can disable these.
_INVARIANT_TRIPWIRES: list[tuple[str, str]] = [
    (r"\b(without (asking|approval|confirming)|don'?t ask|no approval|auto[- ]?(send|apply|submit)"
     r"|stop asking|just send)\b", "approval_gate"),
    (r"\b(delete|purge|wipe|erase|remove permanently|drop the)\b", "never_delete_data"),
    (r"\b(make (it |something )?up|invent|exaggerate|embellish|pretend I|say I have)\b",
     "never_fabricate"),
    (r"\b(clone|deepfake|face[- ]?swap).{0,20}\b(face|photo|likeness|avatar)\b", "no_face_cloning"),
    (r"\b(clone|copy|train|mimic).{0,20}\b(my voice|voice model)\b", "no_voice_cloning"),
]


class AdaptiveSkill(BaseSkill):
    name = "adaptive"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 notify: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._llm = llm
        self._notify = notify or send_telegram
        self._now = clock

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

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "log_signal": self.log_signal, "candidates": self.candidates,
            "propose": self.propose, "confirm": self.confirm, "decline": self.decline,
            "not_now": self.not_now, "retro": self.retro, "contracts": self.contracts,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            # folds into the existing Sunday week-planner conversation
            ScheduledJob(id="adaptive.retro", func=self.retro, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 17, "minute": 30}),
            ScheduledJob(id="adaptive.propose", func=self.propose, trigger="cron",
                         kwargs={"hour": 19}),
        ]

    def contract(self) -> SkillContract:
        """Reads nothing itself — it only proposes rules for OTHER skills to read."""
        return SkillContract(reads_categories=[], hard_invariants=list(UNIVERSAL_INVARIANTS))

    @property
    def _threshold(self) -> int:
        from core.config import get_settings

        return int(get_settings().get("adaptive", "threshold", default=DEFAULT_THRESHOLD))

    # ------------------------------------------------------------- passive logging
    def log_signal(self, skill: str = "", signal_type: str = "", payload: str = "",
                   contradicts: bool = False, **_: Any) -> CommandResult:
        """Record an observation. Deliberately does nothing else."""
        if not skill or not signal_type:
            return CommandResult(text="log_signal needs a skill and signal_type.", ok=False)
        self.mem.log_signal(skill, signal_type, payload or None, contradicts=bool(contradicts),
                            now=self._now())
        return CommandResult(text="noted", data={"logged": True, "acted": False})

    # ------------------------------------------------------------- candidates
    def candidates(self, **_: Any) -> CommandResult:
        rows = self.mem.signal_candidates(self._threshold)
        if not rows:
            return CommandResult(text="No patterns consistent enough to propose yet.",
                                 data={"candidates": []})
        lines = [f"{len(rows)} pattern(s) ready to propose:"]
        for r in rows:
            what = f"{r['signal_type']} ({r['payload']})" if r["payload"] else r["signal_type"]
            lines.append(f"  [{r['id']}] {r['skill']}: {what} ×{r['running_count']}")
        return CommandResult(text="\n".join(lines),
                             data={"candidates": [dict(r) for r in rows]})

    def _rule_for(self, row: dict[str, Any]) -> tuple[str, str]:
        """Turn a repeated signal into (rule_text, category). Deterministic fallback if no LLM."""
        payload = row["payload"] or ""
        guess_cat = {"event_scout": "events", "job_hunter": "jobs", "spaced_rep": "study",
                     "code_tutor": "study", "deal_broker": "flips", "semester_planner": "study",
                     "email_agent": "notifications", "cv_tailor": "cv"}.get(row["skill"], "general")
        fallback = (f"deprioritize {payload} for {row['signal_type'].replace('_', ' ')}"
                    if payload else f"deprioritize {row['signal_type'].replace('_', ' ')}")
        try:
            data = self.llm.chat_json(
                "classify",
                [{"role": "system", "content":
                    "Turn a repeated user behaviour into ONE short standing instruction in the "
                    "user's voice, plus its category. Categories: "
                    f"{', '.join(INSTRUCTION_CATEGORIES)}. Return JSON."},
                 {"role": "user", "content":
                    f"Skill '{row['skill']}' observed '{row['signal_type']}'"
                    f"{f' for {payload}' if payload else ''} {row['running_count']} times in a row."}],
                schema_hint='{"rule": string, "category": string}', temperature=0.0, max_tokens=120)
            rule = str(data.get("rule") or fallback).strip()
            cat = str(data.get("category") or guess_cat).strip().lower()
            if cat not in INSTRUCTION_CATEGORIES:
                cat = guess_cat
            return rule, cat
        except LLMError:
            return fallback, guess_cat

    def propose(self, notify: bool = True, **_: Any) -> CommandResult:
        """Surface candidate rules for Confirm / Reject / Not now — never auto-applies."""
        rows = self.mem.signal_candidates(self._threshold)
        if not rows:
            return CommandResult(text="Nothing new to propose.", data={"proposed": 0})
        proposals = []
        for r in rows:
            rule, cat = self._rule_for(dict(r))
            self.mem.set_signal_status(r["id"], "proposed")
            proposals.append({"id": r["id"], "rule": rule, "category": cat,
                              "count": r["running_count"], "skill": r["skill"]})
        lines = ["🧭 I've noticed some patterns — want these as standing rules?"]
        for p in proposals:
            lines.append(f"\n[{p['id']}] “{p['rule']}”  ({p['category']}, seen {p['count']}×)"
                         f"\n     Confirm / Reject / Not now")
        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={"proposed": len(proposals), "proposals": proposals})

    # ------------------------------------------------------------- boundary + invariants
    @staticmethod
    def violates_invariant(rule_text: str) -> str | None:
        """Return the §0 invariant a rule would breach, or None. No rule can switch these off."""
        for pattern, invariant in _INVARIANT_TRIPWIRES:
            if re.search(pattern, rule_text or "", re.I):
                return invariant
        return None

    def check_boundary(self, rule_text: str, category: str) -> tuple[bool, str]:
        """A rule may only apply if it breaks no invariant AND some skill declares its category."""
        breached = self.violates_invariant(rule_text)
        if breached:
            return False, (f"that would violate the hard invariant '{breached}' (§0) — "
                           f"no instruction can switch that off")
        if category not in INSTRUCTION_CATEGORIES:
            return False, f"'{category}' isn't a known instruction category"
        readers = self.mem.skills_reading(category)
        if not readers:
            return False, (f"no skill declares that it reads '{category}' instructions, so the "
                           f"rule would never apply — it's out of every skill's scope")
        return True, ", ".join(readers)

    # ------------------------------------------------------------- confirm / decline
    def confirm(self, signal_id: int | str = 0, rule: str = "", category: str = "",
                **_: Any) -> CommandResult:
        """Calvin accepts a proposal -> it becomes a Standing Instruction (scope-checked)."""
        row = self.mem.get_signal(int(signal_id))
        if not row:
            return CommandResult(text=f"No pattern {signal_id}.", ok=False)
        if not rule:
            rule, category = self._rule_for(dict(row))
        category = category or self._rule_for(dict(row))[1]

        ok, detail = self.check_boundary(rule, category)
        if not ok:
            return CommandResult(text=f"⛔ Can't add that rule — {detail}.", ok=False,
                                 data={"blocked": detail})
        get_engine().remember(rule, category=category, source="adaptive")
        self.mem.set_signal_status(int(signal_id), "confirmed")
        return CommandResult(
            text=f"✅ Added standing rule: “{rule}” ({category}). Skills reading it: {detail}.",
            data={"rule": rule, "category": category, "readers": detail})

    def decline(self, signal_id: int | str = 0, **_: Any) -> CommandResult:
        """Calvin says no -> recorded as seen-and-declined, never proposed again."""
        if not self.mem.get_signal(int(signal_id)):
            return CommandResult(text=f"No pattern {signal_id}.", ok=False)
        self.mem.set_signal_status(int(signal_id), "declined")
        return CommandResult(text="Noted — I won't suggest that again.",
                             data={"status": "declined"})

    def not_now(self, signal_id: int | str = 0, **_: Any) -> CommandResult:
        """Defer: back to watching, so it can resurface if the pattern keeps holding."""
        if not self.mem.get_signal(int(signal_id)):
            return CommandResult(text=f"No pattern {signal_id}.", ok=False)
        self.mem.set_signal_status(int(signal_id), "watching")
        return CommandResult(text="Fine — I'll keep watching and may ask again later.",
                             data={"status": "watching"})

    # ------------------------------------------------------------- weekly retro
    def retro(self, answer: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Sunday retro. With no answer it asks; with an answer it mines it for candidates."""
        if not answer:
            # A session already open means last week's prompt never got a reply -- say so
            # instead of silently re-asking as if nothing happened (#22: "no expiry notice").
            unanswered = bool(self.mem.kv_get(_RETRO_SESSION_KEY))
            prompt = ("🪞 Weekly retro — quick one: what worked this week, and what didn't? "
                      "(Anything you say here I'll turn into proposed rules, never silent changes.)")
            if unanswered:
                prompt = "🪞 Never heard back on last week's retro — skipping it. " + prompt
            if notify:
                self._notify(prompt)
            self.mem.kv_set(_RETRO_SESSION_KEY, "1")
            return CommandResult(text=prompt, data={"asked": True, "previous_unanswered": unanswered})

        self.mem.kv_set(_RETRO_SESSION_KEY, "")
        try:
            data = self.llm.chat_json(
                "classify",
                [{"role": "system", "content":
                    "From this weekly retro note, extract concrete behaviour changes the assistant "
                    "should adopt, as short standing instructions with categories "
                    f"({', '.join(INSTRUCTION_CATEGORIES)}). Only what the note actually says. "
                    "Return JSON."},
                 {"role": "user", "content": answer}],
                schema_hint='{"rules": [{"rule": string, "category": string}]}',
                temperature=0.1, max_tokens=400)
        except LLMError:
            return CommandResult(text="Couldn't process the retro right now.", ok=False)

        queued = []
        for r in data.get("rules", []):
            rule, cat = str(r.get("rule", "")).strip(), str(r.get("category", "general")).lower()
            if not rule:
                continue
            ok, detail = self.check_boundary(rule, cat if cat in INSTRUCTION_CATEGORIES else "general")
            if not ok:
                queued.append(f"⛔ “{rule}” — {detail}")
                continue
            # retro items go through the SAME proposal queue, not straight in
            self.mem.log_signal("adaptive", "retro_suggestion", rule, now=self._now())
            queued.append(f"• “{rule}” ({cat}) — proposed")
        text = "Retro captured:\n" + "\n".join(queued) if queued else "Nothing actionable in that."
        return CommandResult(text=text, data={"queued": len(queued)})

    # ------------------------------------------------------------- contracts view
    def contracts(self, **_: Any) -> CommandResult:
        rows = self.mem.get_contracts()
        if not rows:
            return CommandResult(text="No skill contracts registered yet.", data={"contracts": []})
        lines = ["📜 Skill contracts (what each skill may be influenced by):"]
        for r in rows:
            reads = r["reads_categories"] or "(nothing)"
            extra = [i for i in r["hard_invariants"].split(",") if i and i not in UNIVERSAL_INVARIANTS]
            lines.append(f"  {r['skill_name']}: reads [{reads}]"
                         + (f" · extra invariants: {', '.join(extra)}" if extra else ""))
        lines.append(f"\nEvery skill is also bound by the §0 invariants: {', '.join(UNIVERSAL_INVARIANTS)}.")
        return CommandResult(text="\n".join(lines), data={"contracts": [dict(r) for r in rows]})


SKILL = AdaptiveSkill()
