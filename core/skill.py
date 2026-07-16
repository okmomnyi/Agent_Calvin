"""Shared Skill interface for AgentOS.

Principle 6 (§0): everything is a Skill. Each capability is a self-contained module in
skills/ implementing this interface (name, commands, scheduled_jobs, handle), auto-
discovered at startup. Adding a capability must never require touching the kernel.

Phase 20 extends the interface with a **Skill Contract**: every skill declares (a) which
Standing Instruction categories it reads, and (b) hard invariants it can never violate
regardless of any instruction. Instructions outside a skill's declared scope are IGNORED,
not applied — that's what lets the agent keep adapting without a tone rule leaking into,
say, Code Tutor's grading logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# Canonical Standing Instruction categories. A rule belongs to exactly one; a skill only
# ever sees rules in the categories its contract declares.
INSTRUCTION_CATEGORIES = [
    "tone",           # how things are written / spoken
    "jobs",           # job hunting & applications
    "cv",             # CV tailoring
    "events",         # event scouting
    "study",          # vault / flashcards / tutor / planner
    "notifications",  # when & how Calvin is contacted
    "flips",          # marketplace deal broking
    "music",          # playback & queueing
    "desktop",        # launching/closing apps on Calvin's laptop
    "general",        # cross-cutting preferences
]

# §0 principles that NO instruction can ever switch off, for any skill, ever.
UNIVERSAL_INVARIANTS = [
    "approval_gate",       # P3 — nothing acts in Calvin's name without confirmation
    "never_delete_data",   # P4
    "never_fabricate",     # P5 — never invent facts about Calvin
    "no_face_cloning",     # P8
    "no_voice_cloning",    # P9
]


@dataclass
class SkillContract:
    """What a skill is allowed to be influenced by, and what it can never be talked into.

    `reads_categories` — Standing Instruction categories this skill consults. Anything else
    is out of scope and must be ignored.
    `hard_invariants` — behaviours this skill can never violate no matter what an
    instruction says. Always includes the universal §0 invariants, plus any per-skill ones
    (e.g. Code Tutor's "never emit a finished assignment").
    """

    reads_categories: list[str] = field(default_factory=list)
    hard_invariants: list[str] = field(default_factory=lambda: list(UNIVERSAL_INVARIANTS))

    def __post_init__(self) -> None:
        for inv in UNIVERSAL_INVARIANTS:          # universal invariants are non-negotiable
            if inv not in self.hard_invariants:
                self.hard_invariants.append(inv)
        unknown = [c for c in self.reads_categories if c not in INSTRUCTION_CATEGORIES]
        if unknown:
            raise ValueError(f"unknown instruction category/ies {unknown}; "
                             f"allowed: {INSTRUCTION_CATEGORIES}")

    def reads(self, category: str) -> bool:
        return category in self.reads_categories


@dataclass
class ScheduledJob:
    """A job the scheduler should register for a skill.

    `trigger`/`kwargs` mirror APScheduler's add_job signature, e.g.
    ScheduledJob(id='email.digest', func=self.daily_digest, trigger='cron', hour=7).
    """

    id: str
    func: Callable[..., Any]
    trigger: str = "interval"
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandResult:
    """Uniform return type for skill actions."""

    text: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


class Skill(Protocol):
    """Structural interface every skill satisfies.

    Implementations live in skills/<name>.py and expose a module-level `SKILL`
    instance (or a `get_skill()` factory) so the registry can discover them.
    """

    name: str

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        """Map action name -> handler. Handlers accept (**payload) and return CommandResult."""
        ...

    def scheduled_jobs(self) -> list[ScheduledJob]:
        """Return jobs to register with the scheduler (may be empty)."""

    def contract(self) -> SkillContract:
        """Declare which instruction categories this skill reads + its hard invariants."""

    def handle(self, action: str, payload: dict[str, Any]) -> CommandResult:
        """Dispatch an action by name to the matching command handler."""


class BaseSkill:
    """Convenience base providing a default handle() that dispatches via commands()."""

    name: str = "base"

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    def contract(self) -> SkillContract:
        """Default contract: reads NO instructions, bound by the universal §0 invariants.

        A skill opts in to being influenced by declaring categories — silence means
        "nothing may reach me", which is the safe default.
        """
        return SkillContract()

    def handle(self, action: str, payload: dict[str, Any]) -> CommandResult:
        handler = self.commands().get(action)
        if handler is None:
            return CommandResult(
                text=f"Skill '{self.name}' has no action '{action}'.", ok=False
            )
        return handler(**payload)
