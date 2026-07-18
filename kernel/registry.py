"""Skill discovery and dispatch for AgentOS.

Auto-discovers every module in the skills/ package that exposes a `SKILL` instance or
a `get_skill()` factory, indexes them by name, and dispatches (skill, action, payload)
calls. This is the seam that keeps Principle 6 true: adding a skill never touches the
kernel — it just gets found here at startup.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import skills as skills_pkg
from core.intent import Intent, IntentRouter, get_router
from core.logging_setup import get_logger
from core.skill import CommandResult, ScheduledJob, Skill

log = get_logger("kernel.registry")


class SkillRegistry:
    """Holds discovered skills and routes intents/commands to them."""

    def __init__(self, router: IntentRouter | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        self.router = router or get_router()

    # ------------------------------------------------------------- discovery
    def discover(self) -> None:
        """Import every skills.* module and register any SKILL / get_skill() it exposes."""
        self._skills.clear()
        for mod_info in pkgutil.iter_modules(skills_pkg.__path__):
            name = mod_info.name
            if name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"skills.{name}")
            except Exception:  # noqa: BLE001 - one bad skill must not down the kernel
                log.exception("Failed to import skill module 'skills.%s'", name)
                continue

            skill = getattr(module, "SKILL", None)
            if skill is None and hasattr(module, "get_skill"):
                try:
                    skill = module.get_skill()
                except Exception:  # noqa: BLE001
                    log.exception("get_skill() failed for 'skills.%s'", name)
                    continue
            if skill is None:
                continue

            # Persist contracts as one batch after discovery.  Calling get_memory() once
            # per skill made an unavailable database cost CONNECT_TIMEOUT for every module
            # (several minutes) before startup finally failed.
            self._skills[skill.name] = skill
        self._register_all_contracts()
        log.info("Discovered %d skill(s): %s", len(self._skills), ", ".join(sorted(self._skills)))

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            log.warning("Duplicate skill name '%s' — overwriting.", skill.name)
        self._skills[skill.name] = skill
        self._register_contract(skill)

    def _register_all_contracts(self) -> None:
        """Persist discovered contracts through one resolved database handle.

        Discovery itself remains useful when PostgreSQL is unavailable (for diagnostics),
        and a failed connection is attempted only once rather than once per skill.
        """
        try:
            from core.memory import get_memory

            memory = get_memory()
        except Exception:  # noqa: BLE001 - discovery must still complete for health output
            log.warning("Could not persist skill contracts: database unavailable")
            return

        for skill in self._skills.values():
            self._register_contract(skill, memory=memory)

    @staticmethod
    def _register_contract(skill: Skill, memory: Any | None = None) -> None:
        """Persist the skill's declared scope (Phase 20) so rules can be boundary-checked."""
        try:
            if memory is None:
                from core.memory import get_memory

                memory = get_memory()

            contract = skill.contract()
            memory.register_contract(skill.name, contract.reads_categories,
                                     contract.hard_invariants)
        except Exception:  # noqa: BLE001 - a contract write must never break discovery
            log.debug("could not persist contract for '%s'", getattr(skill, "name", "?"))

    # ------------------------------------------------------------- accessors
    @property
    def skills(self) -> dict[str, Skill]:
        return dict(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all_scheduled_jobs(self) -> list[ScheduledJob]:
        jobs: list[ScheduledJob] = []
        for skill in self._skills.values():
            try:
                jobs.extend(skill.scheduled_jobs())
            except Exception:  # noqa: BLE001
                log.exception("scheduled_jobs() failed for skill '%s'", skill.name)
        return jobs

    # ------------------------------------------------------------- dispatch
    def dispatch_intent(self, intent: Intent) -> CommandResult:
        """Run a resolved Intent against its target skill, with graceful fallbacks."""
        skill = self._skills.get(intent.skill)
        if skill is None:
            # skill not built yet (later phase) — degrade to a helpful message
            return CommandResult(
                text=(
                    f"That maps to the '{intent.skill}' skill (action '{intent.action}'), "
                    "which isn't wired up yet."
                ),
                data={"intent": intent.name, "skill": intent.skill, "pending": True},
                ok=False,
            )
        try:
            return skill.handle(intent.action, intent.args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Skill '%s' action '%s' raised", intent.skill, intent.action)
            return CommandResult(text=f"'{intent.skill}' failed: {exc}", ok=False)

    def handle_command(self, text: str, *, use_llm: bool = True) -> tuple[Intent, CommandResult]:
        """Full path: raw text -> intent -> skill result."""
        continuation = self._active_continuation(text)
        if continuation is not None:
            return continuation
        intent = self.router.route(text, use_llm=use_llm)
        result = self.dispatch_intent(intent)
        return intent, result

    def _active_continuation(self, text: str) -> tuple[Intent, CommandResult] | None:
        """Continue server-side conversations consistently on every channel.

        Telegram previously did this itself, which left voice, dashboard and REST follow-up
        answers to fall through the general router.  The registry is the shared narrow waist.
        """
        try:
            from core.memory import get_memory

            mem = get_memory()
            flows = (
                ("email_agent.trash_session", "email_trash", "email_agent", "continue_trash", "text"),
                ("cv_tailor.session", "cv_refinement", "cv_tailor", "continue_refinement", "text"),
                ("interview_prep.mock", "mock_answer", "interview_prep", "mock_answer", "answer"),
                ("spaced_rep.session", "quiz_answer", "spaced_rep", "quiz_answer", "answer"),
                ("code_tutor.session", "tutor_continue", "code_tutor", "continue", "text"),
            )
            for key, name, skill, action, arg_name in flows:
                if mem.kv_get(key):
                    intent = Intent(
                        name=name,
                        skill=skill,
                        action=action,
                        args={arg_name: text},
                        confidence=1.0,
                        via="session",
                    )
                    return intent, self.dispatch_intent(intent)
        except Exception:  # noqa: BLE001 - routing still works if continuity storage is down
            log.debug("could not inspect active conversational sessions", exc_info=True)
        return None
