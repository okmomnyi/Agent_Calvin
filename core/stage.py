"""Stage directive protocol (Phase 38).

Shared shape for the self-driving stage's WS payload. A StageDirective describes WHAT the
frontend Director should show, never HOW: no colors, no animation timings, no DOM
references — those live entirely in frontend/src/design/tokens.ts and the Director itself.
core/presenter.py builds these from what skills already compute; this module only defines
the wire shape plus the in-process broadcast bus that lets a catalyst (world_news's
corroboration-gated check_breaking, the one exception to pull-only delivery) push one to
connected browser stages BETWEEN turns, not only as a reply to something Calvin said.

Mirrors frontend/src/core/stageTypes.ts's StageDirective/Widget union exactly — a change to
one shape is a change to both, same discipline core/session.py's Turn already holds with
frontend/src/core/types.ts.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Union

from core.logging_setup import get_logger

log = get_logger("core.stage")

Accent = Literal["primary", "alert"]
Transition = Literal["bloom", "swap", "settle"]


@dataclass
class ChartWidget:
    """A price series for ONE asset. `series` must be real ticks from the source that
    already computed them (skills/markets.py) — never synthesized, interpolated, or
    smoothed. `as_of` + `delayed_label` are mandatory on every instance: crypto is
    real-time, fx/commodities/stocks are the free feed's ~15m delay, and both must say so
    on the widget itself, not just somewhere upstream of it (§0 P5's honesty applies to the
    product surface, not only to what the model is told)."""

    asset: str
    klass: str  # crypto | equity | fx | commodity | nse
    range: str  # 1d | 1w | 1m
    series: list[dict[str, float]]  # [{"t": epoch_seconds, "v": price}, ...]
    as_of: float
    delayed_label: str
    source: str
    type: str = "chart"


@dataclass
class ArticlesWidget:
    """`items[].image` is a feed thumbnail URL or None — NEVER populated by an image
    search. A missing image is the typed fallback card client-side, not a broken <img> and
    not a substituted picture from anywhere else."""

    topic: str
    items: list[dict[str, Any]]  # {title, source, url, published, image: str|None}
    type: str = "articles"


@dataclass
class TickerWidget:
    items: list[dict[str, Any]]  # {symbol, price, change_pct, as_of}
    type: str = "ticker"


@dataclass
class FactWidget:
    title: str
    stat: str
    sub: str
    sources: list[str] = field(default_factory=list)
    type: str = "fact"


@dataclass
class MapWidget:
    region: str
    markers: list[dict[str, Any]] = field(default_factory=list)  # {lat, lng, label}
    type: str = "map"


Widget = Union[ChartWidget, ArticlesWidget, TickerWidget, FactWidget, MapWidget]


@dataclass
class StageDirective:
    """`focus=None` + `widgets=[]` is the calm idle HUD — Director degrades to Phase 36
    when there is nothing to show, never fabricates a placeholder scene."""

    focus: str | None
    transition: Transition
    widgets: list[Widget] = field(default_factory=list)
    headline: str | None = None
    accent: Accent = "primary"
    ttl_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus": self.focus,
            "headline": self.headline,
            "accent": self.accent,
            "transition": self.transition,
            "ttl_s": self.ttl_s,
            "widgets": [asdict(w) for w in self.widgets],
        }


def idle_directive() -> StageDirective:
    """No directive → the calm Phase 36 HUD. Modeled explicitly (rather than `None`) so a
    presenter that has genuinely decided "nothing needs Calvin" can still say so with a
    settle transition, distinct from "the presenter itself failed"."""
    return StageDirective(focus=None, transition="settle", widgets=[])


class StageBus:
    """In-process registry of connected /ws/voice sockets so a catalyst can push a
    StageDirective between turns.

    Phase 38's catalyst (world_news.check_breaking) is an UNQUEUED, interval APScheduler
    job — it runs in the same process as the API (see world_news.scheduled_jobs()), same
    convention as every other light job here. But AsyncIOScheduler executes a plain sync
    callable via the default executor (a worker THREAD), not the event loop itself, so
    `push()` is the thread-safe entry point: it hands the broadcast coroutine to the loop
    captured at kernel startup via `run_coroutine_threadsafe`, rather than assuming it is
    already running on that loop. A push before any browser has connected, or after the
    kernel event loop hasn't been bound yet (unit tests constructing a bare StageBus), is a
    silent no-op — a catalyst must never raise just because nobody's watching the stage.
    """

    def __init__(self) -> None:
        self._sockets: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, ws: Any) -> None:
        self._sockets.add(ws)

    def unregister(self, ws: Any) -> None:
        self._sockets.discard(ws)

    async def _broadcast(self, directive: StageDirective) -> None:
        payload = {"ok": True, "text": "", "directive": directive.to_dict()}
        dead = []
        for ws in list(self._sockets):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 - one dead socket must not block the rest
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    def push(self, directive: StageDirective) -> None:
        """Thread-safe. Callable from a scheduler worker thread or the event loop itself."""
        if self._loop is None or not self._sockets:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(directive), self._loop)
        except Exception:  # noqa: BLE001 - a catalyst push failing must never break the caller
            log.warning("stage: pushing a catalyst directive failed", exc_info=True)


_bus: StageBus | None = None


def get_stage_bus() -> StageBus:
    global _bus
    if _bus is None:
        _bus = StageBus()
    return _bus
