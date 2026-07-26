"""Stage voice-steering pseudo-skill (Phase 38).

"Voice issues directives, doesn't poke the DOM" (the brief's own words): "show oil", "pin
that", "back to idle" all land here as ordinary routed commands, exactly like any other
skill action, and carry no more authority than any other reply — a directive is display
data only, never a high-tier action, and nothing here ever touches core/approvals.py.

`focus()` doesn't build a StageDirective itself; it decides WHICH existing skill (markets
or world_news) `subject` belongs to and hands that decision to core/presenter.py via
CommandResult.data — the actual widget-building stays inside markets.display()/
world_news.display(), so this skill adds no new data source and no fabrication surface.
`pin`/`unpin` are pure client-side signals (which widget is frozen — the server has no
state to change), so their handlers only need to exist and reply; `idle` is the one action
that genuinely asks the presenter for the calm HUD.
"""

from __future__ import annotations

from typing import Any, Callable

from core.skill import BaseSkill, CommandResult, SkillContract


class StageSkill(BaseSkill):
    name = "stage"

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"focus": self.focus, "pin": self.pin, "unpin": self.unpin, "idle": self.idle}

    def contract(self) -> SkillContract:
        """Reads no standing instructions — steering the stage isn't a preference a tone/
        general rule should reach into; bound only by the universal §0 invariants."""
        return SkillContract()

    def focus(self, subject: str = "", **_: Any) -> CommandResult:
        """Markets is tried first: a configured instrument name is the more specific
        vocabulary, and a news category word (kenya/world/sports/...) never collides with
        one. Neither resolving is an honest, expected outcome for an offhand phrase — it
        replies plainly rather than guessing, and core/presenter.py omits the stage change
        entirely (`stage_kind: "none"`)."""
        from skills.markets import _instruments, _resolve_asset

        cleaned = (subject or "").strip()
        if not cleaned:
            return CommandResult(text="Show you what, exactly?", ok=False,
                                 data={"stage_kind": "none"})

        inst, _category = _resolve_asset(cleaned, _instruments())
        if inst is not None:
            return CommandResult(text=f"Here's {inst.name}.", ok=True,
                                 data={"stage_kind": "markets", "asset": cleaned})

        from skills.world_news import WorldNewsSkill

        # _resolve_topic() only reads config (category keys + the alias table) -- no
        # network, safe to call on a scratch instance.
        if WorldNewsSkill()._resolve_topic(cleaned) is not None:
            return CommandResult(text=f"Here's what's up with {cleaned}.", ok=True,
                                 data={"stage_kind": "world_news", "topic": cleaned})

        return CommandResult(text=f'I don\'t have a feed for "{cleaned}".', ok=False,
                             data={"stage_kind": "none"})

    def pin(self, **_: Any) -> CommandResult:
        return CommandResult(text="Pinned — the rest of the stage keeps moving around it.",
                             ok=True, data={"stage_kind": "pin"})

    def unpin(self, **_: Any) -> CommandResult:
        return CommandResult(text="Unpinned.", ok=True, data={"stage_kind": "unpin"})

    def idle(self, **_: Any) -> CommandResult:
        return CommandResult(text="Back to idle.", ok=True, data={"stage_kind": "idle"})


SKILL = StageSkill()
