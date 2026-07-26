"""Stage directive protocol: wire shape + the catalyst broadcast bus (Phase 38)."""

from __future__ import annotations

import asyncio
import threading
import time

from core.stage import (ArticlesWidget, ChartWidget, FactWidget, StageBus, StageDirective,
                        TickerWidget, get_stage_bus, idle_directive)


def test_idle_directive_has_no_focus_and_no_widgets():
    d = idle_directive()
    assert d.focus is None
    assert d.widgets == []
    assert d.transition == "settle"


def test_to_dict_serializes_widgets_and_top_level_fields():
    chart = ChartWidget(asset="Bitcoin", klass="crypto", range="1d",
                        series=[{"t": 1.0, "v": 2.0}], as_of=1.0,
                        delayed_label="real-time", source="coingecko")
    d = StageDirective(focus="Bitcoin", transition="bloom", accent="primary",
                       headline="up 2%", widgets=[chart]).to_dict()
    assert d["focus"] == "Bitcoin"
    assert d["accent"] == "primary"
    assert d["transition"] == "bloom"
    assert d["widgets"] == [{
        "asset": "Bitcoin", "klass": "crypto", "range": "1d",
        "series": [{"t": 1.0, "v": 2.0}], "as_of": 1.0,
        "delayed_label": "real-time", "source": "coingecko", "type": "chart",
    }]


def test_to_dict_handles_every_widget_type():
    d = StageDirective(focus="x", transition="swap", widgets=[
        ArticlesWidget(topic="world", items=[{"title": "t", "source": "s", "url": "u",
                                              "published": 1.0, "image": None}]),
        TickerWidget(items=[{"symbol": "BTC", "price": 1.0, "change_pct": 0.1, "as_of": 1.0}]),
        FactWidget(title="t", stat="s", sub="sub", sources=["Reuters"]),
    ]).to_dict()
    types = [w["type"] for w in d["widgets"]]
    assert types == ["articles", "ticker", "fact"]


def test_get_stage_bus_returns_a_singleton():
    assert get_stage_bus() is get_stage_bus()


# ------------------------------------------------------------------ StageBus (thread-safe push)
class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.received: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        if self.fail:
            raise ConnectionError("socket is gone")
        self.received.append(payload)


def test_push_is_a_silent_no_op_with_no_loop_bound():
    bus = StageBus()
    bus.register(_FakeSocket())
    bus.push(idle_directive())  # must not raise


def test_push_is_a_silent_no_op_with_no_sockets_connected():
    bus = StageBus()
    loop = asyncio.new_event_loop()
    bus.bind_loop(loop)
    bus.push(idle_directive())  # must not raise, nothing to deliver to
    loop.close()


def _run_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def test_push_delivers_to_every_connected_socket_from_another_thread():
    """The realistic case: check_breaking() fires on an APScheduler worker thread, not the
    event loop thread -- push() must still get the payload to a connected browser."""
    bus = StageBus()
    loop, thread = _run_loop_in_thread()
    try:
        bus.bind_loop(loop)
        a, b = _FakeSocket(), _FakeSocket()
        bus.register(a)
        bus.register(b)

        directive = StageDirective(focus="Conflict", transition="bloom", accent="alert",
                                   widgets=[FactWidget(title="CORROBORATION", stat="4",
                                                       sub="", sources=["Reuters"])])
        bus.push(directive)

        for _ in range(50):
            if a.received and b.received:
                break
            time.sleep(0.02)

        assert a.received and a.received[0]["directive"]["accent"] == "alert"
        assert b.received and b.received[0]["ok"] is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_push_drops_a_dead_socket_without_blocking_the_rest():
    bus = StageBus()
    loop, thread = _run_loop_in_thread()
    try:
        bus.bind_loop(loop)
        dead, alive = _FakeSocket(fail=True), _FakeSocket()
        bus.register(dead)
        bus.register(alive)

        bus.push(idle_directive())

        for _ in range(50):
            if alive.received:
                break
            time.sleep(0.02)

        assert alive.received
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
