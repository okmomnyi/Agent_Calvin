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


# System-owned fallbacks for consequential commands.  Skills can declare a more complete
# ``action_tiers`` mapping; this table protects existing skills while that metadata is being
# adopted.  A model never sees or writes these values.
_ACTION_TIERS: dict[tuple[str, str], str] = {
    ("email_agent", "trash"): "low",
    ("email_agent", "continue_trash"): "low",
    ("email_agent", "restore"): "low",
    ("email_agent", "continue_send"): "high",
    ("job_hunter", "approve"): "high",
    ("form_assist", "submit"): "high",
    ("deal_broker", "confirm_purchase"): "high",
    ("desktop", "open"): "medium",
    ("desktop", "close"): "medium",
    ("desktop", "focus"): "medium",
    ("music", "playlist"): "medium",
    ("music", "playlist_remove"): "medium",
    ("music", "play"): "low",
    ("music", "pause"): "low",
    ("music", "next"): "low",
    ("music", "previous"): "low",
    ("music", "volume"): "low",
    # Phase 36: explicitly declared even though "trivial" is already the default, so a
    # reader of this table sees it was a deliberate choice for each — B4/B5/B6's own tier.
    ("web_open", "open"): "trivial",
    ("youtube", "play"): "trivial",
    ("weather", "current"): "trivial",
    # Placing a call speaks in Calvin's voice toward a real person — `high`, never learnable
    # (LEARNABLE_TIERS excludes it structurally; see core/approvals.py). Answering/ending an
    # already-ringing call acts on something already happening, not something initiated in
    # his name, so it's `low` — reversible, and fine to learn.
    ("phone", "call"): "high",
    ("phone", "continue_call"): "high",
    ("phone", "answer"): "low",
    ("phone", "hangup"): "low",
}

_PLAN_INTERNAL_ACTIONS = {
    "continue", "continue_refinement", "continue_send", "continue_trash", "continue_call",
    "drill_check", "mock_answer", "mocklab_submit", "quiz_answer", "session_tick",
}


class SkillRegistry:
    """Holds discovered skills and routes intents/commands to them."""

    def __init__(self, router: IntentRouter | None = None, *, orchestrator: Any | None = None,
                 planning_enabled: bool | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        self.router = router or get_router()
        self._orchestrator = orchestrator
        self._planning_enabled = planning_enabled

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

    def manifest(self) -> list[dict[str, Any]]:
        """Return the real command surface available to the planner.

        The list is generated from registered skills, so a proposed pair can be checked by
        exact membership.  ``plan_exclude`` is owned by the skill and hides continuations or
        other commands that should never be selected out of context.
        """
        out: list[dict[str, Any]] = []
        for name, skill in sorted(self._skills.items()):
            excluded = set(getattr(skill, "plan_exclude", ()))
            for action, fn in sorted(skill.commands().items()):
                if action in excluded or action in _PLAN_INTERNAL_ACTIONS or action.startswith("_"):
                    continue
                doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
                out.append({
                    "skill": name,
                    "action": action,
                    "doc": doc[0] if doc else "",
                    "args": dict(getattr(fn, "_arg_hints", {}) or {}),
                })
        return out

    def action_tier(self, skill_name: str, action: str) -> str:
        """Resolve authority from code-owned metadata, never from a proposed plan."""
        skill = self._skills.get(skill_name)
        declared = getattr(skill, "action_tiers", {}) if skill is not None else {}
        tier = declared.get(action, _ACTION_TIERS.get((skill_name, action), "trivial"))
        if tier not in {"trivial", "low", "medium", "high"}:
            log.warning("Invalid tier %r for %s.%s; failing closed to high", tier,
                        skill_name, action)
            return "high"
        return tier

    def is_queued_action(self, skill_name: str, action: str) -> bool:
        """Whether this command is already declared heavy by a queued scheduled job."""
        skill = self._skills.get(skill_name)
        if skill is None:
            return False
        try:
            return any(job.queued and job.skill == skill_name and job.action == action
                       for job in skill.scheduled_jobs())
        except Exception:  # noqa: BLE001 - metadata failure falls back to safe inline dispatch
            log.exception("scheduled_jobs() failed while checking %s.%s", skill_name, action)
            return False

    @property
    def orchestrator(self):
        """Lazily construct the planner so ordinary command startup remains unchanged."""
        if self._orchestrator is None:
            from core.orchestrator import Orchestrator

            self._orchestrator = Orchestrator(registry=self)
        return self._orchestrator

    def planning_enabled(self) -> bool:
        if self._planning_enabled is not None:
            return self._planning_enabled
        try:
            from core.config import get_settings

            return bool(get_settings().get("orchestrator", "enabled", default=True))
        except Exception:  # noqa: BLE001 - config trouble must preserve command routing
            return False

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

    def handle_command(self, text: str, *, use_llm: bool = True,
                       channel: str = "cli") -> tuple[Intent, CommandResult]:
        """Full path: raw text -> keyword dispatch or goal plan or single dispatch."""
        if self.planning_enabled():
            try:
                from core.orchestrator import is_plan_reply

                reply = self.orchestrator.handle_reply(text) if is_plan_reply(text) else None
                if reply is not None:
                    intent = Intent("plan_reply", "orchestrator", "reply", {"text": text},
                                    confidence=1.0, via="plan")
                    return intent, reply
            except Exception:  # noqa: BLE001 - planner state must not break ordinary routing
                log.debug("could not inspect active plan", exc_info=True)
        continuation = self._active_continuation(text)
        if continuation is not None:
            return continuation

        # Keyword rules always win.  The unmatched keyword-only fallback has confidence .2;
        # a real table hit has .9, so no private router internals need to leak into the kernel.
        fast = self.router.route(text, use_llm=False)
        if fast.confidence >= 0.9:
            return fast, self.dispatch_intent(fast)

        if self.planning_enabled():
            from core.orchestrator import should_plan

            if should_plan(text):
                result = self.orchestrator.run(text, channel=channel)
                intent = Intent("plan", "orchestrator", "run", {"goal": text},
                                confidence=1.0, via="plan")
                return intent, result

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
                ("phone.call_session", "phone_confirm", "phone", "continue_call", "text"),
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


_default_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """Shared, discovered registry.

    Added for the catalogue router (core.intent), which needs to know what the system can
    actually DO in order to route to it. Discovery runs once and is cached; callers that need
    a private instance still construct SkillRegistry() directly.
    """
    global _default_registry
    if _default_registry is None:
        reg = SkillRegistry()
        reg.discover()
        _default_registry = reg
    return _default_registry
