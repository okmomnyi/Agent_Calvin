"""Persona skill (Phase 4).

Exposes the persona engine to the intent router / Telegram / voice: standing instructions
(remember / forget / instructions), answering as Calvin (ask), browsing facts, and the
learning loop (nightly distill of edits, weekly style regeneration). Nothing here ever
invents facts — answer() returns NEEDS_INPUT when something is unknown (§0).
"""

from __future__ import annotations

from typing import Any, Callable

from core.logging_setup import get_logger
from core.notify import send_telegram
from core.persona_store import NEEDS_INPUT, PersonaEngine, get_engine
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.persona")


class PersonaSkill(BaseSkill):
    name = "persona"

    def __init__(self, engine: PersonaEngine | None = None,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._engine = engine
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

    @property
    def engine(self) -> PersonaEngine:
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "remember": self.remember,
            "forget": self.forget,
            "instructions": self.list_instructions,
            "answer": self.answer,
            "ask": self.answer,
            "facts": self.facts,
            "learn": self.learn,
            "regenerate_style": self.regenerate_style,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="persona.distill", func=self.learn, trigger="cron",
                         kwargs={"hour": 2, "minute": 30}),
            ScheduledJob(id="persona.style", func=self.regenerate_style, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 3}),
        ]

    # ------------------------------------------------------------- standing instructions
    def remember(self, instruction: str = "", **_: Any) -> CommandResult:
        if not instruction.strip():
            return CommandResult(text="Remember what? Give me an instruction.", ok=False)
        self.engine.remember(instruction.strip())
        return CommandResult(text=f"Got it — I'll remember: “{instruction.strip()}”.",
                             data={"instructions": self.engine.instructions()})

    def forget(self, instruction: str = "", **_: Any) -> CommandResult:
        if not instruction.strip():
            return CommandResult(text="Forget what? Give me the instruction to drop.", ok=False)
        self.engine.forget(instruction.strip())
        return CommandResult(text=f"Dropped: “{instruction.strip()}”.",
                             data={"instructions": self.engine.instructions()})

    def list_instructions(self, **_: Any) -> CommandResult:
        items = self.engine.instructions()
        if not items:
            return CommandResult(text="No standing instructions yet. Add one with /remember.")
        body = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
        return CommandResult(text=f"Standing instructions:\n{body}", data={"instructions": items})

    # ------------------------------------------------------------- answer as Calvin
    def answer(self, question: str = "", context: str = "", **_: Any) -> CommandResult:
        if not question.strip():
            return CommandResult(text="Ask me a question to answer as you.", ok=False)
        ans = self.engine.answer(question.strip(), context=context)
        if ans.needs_input:
            return CommandResult(
                text=f"I don't have a verified answer for that. {ans.gap}",
                data={"needs_input": True, "gap": ans.gap}, ok=False,
            )
        return CommandResult(text=ans.text, data={"facts_used": ans.facts_used})

    # ------------------------------------------------------------- facts browsing
    def facts(self, category: str = "", **_: Any) -> CommandResult:
        rows = self.engine.get_facts(category or None)
        if not rows:
            return CommandResult(text="No persona facts yet — run `manage.py persona-init`.")
        lines = []
        for r in rows:
            mark = "✓" if r["verified"] else "?"
            lines.append(f"{mark} [{r['category']}] {r['key']}: {r['value']}")
        unverified = [r for r in rows if not r["verified"]]
        head = f"{len(rows)} fact(s)" + (f", {len(unverified)} awaiting confirmation" if unverified else "")
        return CommandResult(text=f"{head}:\n" + "\n".join(lines), data={"count": len(rows)})

    # ------------------------------------------------------------- learning loop
    def learn(self, notify: bool = True, **_: Any) -> CommandResult:
        """Distill candidate facts from Calvin's edits; queue them for confirmation."""
        candidates = self.engine.distill_edits()
        if not candidates:
            return CommandResult(text="No new candidate facts from recent edits.", data={"candidates": 0})
        body = "\n".join(f"- [{c['category']}] {c['key']}: {c['value']}" for c in candidates)
        msg = (f"🧠 I learned {len(candidates)} possible new fact(s) from your edits "
               f"(unverified — confirm with /facts):\n{body}")
        if notify:
            self._notify(msg)
        return CommandResult(text=msg, data={"candidates": len(candidates)})

    def regenerate_style(self, **_: Any) -> CommandResult:
        profile = self.engine.regenerate_style()
        return CommandResult(text=profile or "Not enough approved samples to build a style profile yet.",
                             data={"has_style": bool(profile)})


SKILL = PersonaSkill()
