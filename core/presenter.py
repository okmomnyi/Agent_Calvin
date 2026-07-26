"""Stage presenter (Phase 38): maps the current turn's (Intent, CommandResult) to a
StageDirective, if there is one.

A thin presenter, not a rewrite: this module owns NO data of its own. It asks
skills/markets.py's `display()` for chart/ticker widgets and skills/world_news.py's
`display()` for article widgets — both already computed from what those skills fetch for
their ordinary commands — and shapes whatever comes back into a StageDirective. Returning
None means "this turn doesn't change the stage" (kernel/app.py then sends no `directive`
key at all), which is different from `idle_directive()` ("explicitly go back to idle") —
an unrelated skill running (code_tutor, email_agent, ...) must leave whatever the stage is
already showing alone, not snap it to idle on every unrelated reply.
"""

from __future__ import annotations

from typing import Any

from core.intent import Intent
from core.logging_setup import get_logger
from core.skill import CommandResult
from core.stage import StageDirective, idle_directive

log = get_logger("core.presenter")


def _markets_skill() -> Any:
    from skills.markets import SKILL

    return SKILL


def _news_skill() -> Any:
    from skills.world_news import SKILL

    return SKILL


def _subject_hint(intent: Intent, text: str) -> str:
    """The best available guess at what the turn was about — a routed rule's captured
    argument first (asset/topic/categories/query/text, whichever the matching rule used),
    falling back to the raw command text."""
    for key in ("asset", "topic", "categories", "query", "text"):
        val = intent.args.get(key)
        if val:
            return str(val)
    return text or ""


def _focus_label(widgets: list[Any], fallback: str) -> str:
    for widget in widgets:
        if getattr(widget, "type", "") == "chart":
            return widget.asset
    return fallback


def build_directive(intent: Intent, result: CommandResult, *, text: str = "") -> StageDirective | None:
    """None -> leave the stage as it is. A real StageDirective (including the explicit
    idle one) -> the Director re-choreographs. Never raises: a presenter failure must
    degrade to "don't touch the stage", exactly like an un-buildable widget degrades to
    "omit it" -- see each helper below.
    """
    try:
        if intent.skill == "stage":
            return _from_stage_skill(result)
        if intent.skill == "markets" and intent.action in ("snapshot", "display"):
            return _from_markets(_subject_hint(intent, text))
        if intent.skill == "world_news" and intent.action in ("whats_up", "display"):
            return _from_world_news(_subject_hint(intent, text))
    except Exception:  # noqa: BLE001 - a directive failing to build must never break the reply
        log.warning("presenter: building a stage directive failed", exc_info=True)
        return None
    return None


def _from_stage_skill(result: CommandResult) -> StageDirective | None:
    kind = (result.data or {}).get("stage_kind")
    if kind == "idle":
        return idle_directive()
    if kind in ("pin", "unpin", "none", None):
        # pin/unpin are pure client-side signals (see skills/stage.py); "none" means the
        # spoken subject didn't resolve to anything this presenter can show -- omit rather
        # than guess, per Phase 38's "un-buildable widget -> omitted, never faked".
        return None
    if kind == "markets":
        return _from_markets(str(result.data.get("asset", "")))
    if kind == "world_news":
        return _from_world_news(str(result.data.get("topic", "")))
    return None


def _from_markets(asset_hint: str) -> StageDirective | None:
    widgets = _markets_skill().display(asset=asset_hint)
    if not widgets:
        return None
    return StageDirective(focus=_focus_label(widgets, "Markets"), transition="bloom",
                          widgets=widgets)


def _from_world_news(topic_hint: str) -> StageDirective | None:
    widget = _news_skill().display(topic=topic_hint)
    if widget is None:
        return None
    return StageDirective(focus=widget.topic, transition="bloom", widgets=[widget])
