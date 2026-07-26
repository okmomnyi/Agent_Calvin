"""core/presenter.py: maps (Intent, CommandResult) -> StageDirective|None.

Presenter owns no data of its own -- these tests inject fake markets/world_news skills so
nothing here ever calls the real fetchers, and assert the ROUTING/shaping logic: which
turns touch the stage at all, and how a skill's own display() output becomes a directive.
"""

from __future__ import annotations

from core.intent import Intent
from core.presenter import build_directive
from core.skill import CommandResult
from core.stage import ArticlesWidget, ChartWidget, TickerWidget


def _intent(skill: str, action: str, **args) -> Intent:
    return Intent(name=f"{skill}.{action}", skill=skill, action=action, args=args)


def test_unrelated_skill_returns_none_and_leaves_the_stage_untouched():
    intent = _intent("email_agent", "check")
    result = CommandResult(text="3 new emails")
    assert build_directive(intent, result) is None


# ------------------------------------------------------------------ via the stage skill
def test_stage_idle_returns_the_explicit_idle_directive():
    intent = _intent("stage", "idle")
    result = CommandResult(text="Back to idle.", data={"stage_kind": "idle"})
    directive = build_directive(intent, result)
    assert directive is not None
    assert directive.focus is None
    assert directive.widgets == []
    assert directive.transition == "settle"


def test_stage_pin_and_unpin_return_none_pure_client_side():
    for kind in ("pin", "unpin"):
        intent = _intent("stage", kind)
        result = CommandResult(text="ok", data={"stage_kind": kind})
        assert build_directive(intent, result) is None


def test_stage_focus_none_kind_returns_none():
    intent = _intent("stage", "focus", subject="underwater basket weaving")
    result = CommandResult(text="no feed", ok=False, data={"stage_kind": "none"})
    assert build_directive(intent, result) is None


def test_stage_focus_with_no_data_at_all_returns_none():
    """A stage.* action whose result never set stage_kind (shouldn't happen, but a
    presenter helper must degrade rather than crash on a missing key)."""
    intent = _intent("stage", "focus", subject="x")
    result = CommandResult(text="ok")
    assert build_directive(intent, result) is None


class _FakeMarkets:
    def __init__(self, widgets):
        self._widgets = widgets
        self.calls = []

    def display(self, asset="", **_):
        self.calls.append(asset)
        return self._widgets


class _FakeNews:
    def __init__(self, widget):
        self._widget = widget
        self.calls = []

    def display(self, topic="", **_):
        self.calls.append(topic)
        return self._widget


def test_stage_focus_markets_builds_a_bloom_directive_focused_on_the_chart_asset(monkeypatch):
    chart = ChartWidget(asset="Gold", klass="commodity", range="1d", series=[{"t": 1.0, "v": 2.0}],
                        as_of=1.0, delayed_label="~15m delayed (free feed)", source="yahoo")
    fake = _FakeMarkets([TickerWidget(items=[]), chart])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)

    intent = _intent("stage", "focus", subject="gold")
    result = CommandResult(text="Here's Gold.", data={"stage_kind": "markets", "asset": "gold"})
    directive = build_directive(intent, result)

    assert directive is not None
    assert directive.focus == "Gold"
    assert directive.transition == "bloom"
    assert directive.widgets == [TickerWidget(items=[]), chart]
    assert fake.calls == ["gold"]


def test_stage_focus_markets_with_no_widgets_returns_none(monkeypatch):
    fake = _FakeMarkets([])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)
    intent = _intent("stage", "focus", subject="gold")
    result = CommandResult(text="ok", data={"stage_kind": "markets", "asset": "gold"})
    assert build_directive(intent, result) is None


def test_stage_focus_markets_falls_back_to_a_generic_focus_label_with_no_chart(monkeypatch):
    """A resolved asset whose series wasn't fetchable still yields a ticker-only directive
    -- 'Markets' generic label rather than pretending a chart is there."""
    fake = _FakeMarkets([TickerWidget(items=[{"symbol": "Gold", "price": 1.0,
                                              "change_pct": 0.1, "as_of": 1.0}])])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)
    intent = _intent("stage", "focus", subject="gold")
    result = CommandResult(text="ok", data={"stage_kind": "markets", "asset": "gold"})
    directive = build_directive(intent, result)
    assert directive.focus == "Markets"


def test_stage_focus_world_news_builds_a_bloom_directive_focused_on_the_topic(monkeypatch):
    widget = ArticlesWidget(topic="🌍 World & Conflicts", items=[
        {"title": "t", "source": "s", "url": "u", "published": 1.0, "image": None}])
    fake = _FakeNews(widget)
    monkeypatch.setattr("core.presenter._news_skill", lambda: fake)

    intent = _intent("stage", "focus", subject="world")
    result = CommandResult(text="Here's what's up.", data={"stage_kind": "world_news",
                                                            "topic": "world"})
    directive = build_directive(intent, result)

    assert directive is not None
    assert directive.focus == "🌍 World & Conflicts"
    assert directive.widgets == [widget]
    assert fake.calls == ["world"]


def test_stage_focus_world_news_with_no_widget_returns_none(monkeypatch):
    fake = _FakeNews(None)
    monkeypatch.setattr("core.presenter._news_skill", lambda: fake)
    intent = _intent("stage", "focus", subject="world")
    result = CommandResult(text="ok", data={"stage_kind": "world_news", "topic": "world"})
    assert build_directive(intent, result) is None


# ------------------------------------------------------------------ direct routing (no stage skill)
def test_markets_snapshot_direct_routing_uses_a_blank_asset_hint(monkeypatch):
    fake = _FakeMarkets([TickerWidget(items=[{"symbol": "BTC", "price": 1.0,
                                              "change_pct": 1.0, "as_of": 1.0}])])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)
    intent = _intent("markets", "snapshot")
    result = CommandResult(text="snapshot text")
    directive = build_directive(intent, result, text="what's the market doing")
    assert directive is not None
    assert fake.calls == ["what's the market doing"]


def test_markets_snapshot_direct_routing_prefers_an_asset_arg_over_raw_text(monkeypatch):
    fake = _FakeMarkets([])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)
    intent = _intent("markets", "display", asset="oil")
    build_directive(intent, CommandResult(text="ok"), text="show oil please")
    assert fake.calls == ["oil"]


def test_world_news_whats_up_direct_routing_uses_the_categories_arg(monkeypatch):
    widget = ArticlesWidget(topic="Kenya", items=[])
    fake = _FakeNews(widget)
    monkeypatch.setattr("core.presenter._news_skill", lambda: fake)
    intent = _intent("world_news", "whats_up", categories="kenya")
    directive = build_directive(intent, CommandResult(text="ok"), text="what's up in kenya")
    assert fake.calls == ["kenya"]
    assert directive.focus == "Kenya"


def test_an_action_outside_the_allowed_set_is_ignored(monkeypatch):
    """markets.check_breaking-equivalent guard: only snapshot/display (and world_news's
    whats_up/display) are wired -- an unrelated internal action on those skills must not
    accidentally light up the stage."""
    fake = _FakeMarkets([TickerWidget(items=[{"symbol": "x", "price": 1.0,
                                              "change_pct": 1.0, "as_of": 1.0}])])
    monkeypatch.setattr("core.presenter._markets_skill", lambda: fake)
    intent = _intent("markets", "some_internal_action")
    assert build_directive(intent, CommandResult(text="ok")) is None
    assert fake.calls == []


def test_a_presenter_exception_degrades_to_no_stage_change(monkeypatch):
    class _BoomMarkets:
        def display(self, **_):
            raise RuntimeError("boom")

    monkeypatch.setattr("core.presenter._markets_skill", lambda: _BoomMarkets())
    intent = _intent("markets", "snapshot")
    assert build_directive(intent, CommandResult(text="ok")) is None
