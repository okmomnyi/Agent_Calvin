"""Kernel tests: skill discovery, graceful degradation for unbuilt skills, voice formatting."""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from core.intent import Intent, IntentRouter
from core.skill import CommandResult
from kernel.app import to_spoken
from kernel.registry import SkillRegistry
from manage import _mask_dsn


def test_discovery_finds_chat_skill(fake_llm):
    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    assert "chat" in reg.skills


def test_discovery_attempts_an_unavailable_contract_store_only_once(fake_llm, monkeypatch):
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        raise RuntimeError("database offline")

    monkeypatch.setattr("core.memory.get_memory", unavailable)
    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()

    assert "chat" in reg.skills
    assert calls == 1


def test_unbuilt_skill_degrades_gracefully(fake_llm):
    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    # 'router'/'approvals' are conceptual intent targets with no skill module — dispatching
    # to a name the registry never discovers must degrade gracefully, not crash.
    intent = Intent(name="summarize", skill="router", action="summarize")
    result = reg.dispatch_intent(intent)
    assert result.ok is False
    assert result.data.get("pending") is True
    assert "router" in result.text


def test_to_spoken_strips_markdown_and_shortens():
    md = "# Header\nHere is **bold** and `code` and a [link](http://x).\n\nSecond para."
    spoken = to_spoken(md)
    assert "**" not in spoken
    assert "`" not in spoken
    assert "#" not in spoken
    assert "http://x" not in spoken
    assert "link" in spoken


def test_to_spoken_truncates_long_text():
    long = "word " * 400
    out = to_spoken(long, max_chars=100)
    assert len(out) <= 101
    assert out.endswith("…")


def test_remote_command_requires_shared_token(monkeypatch):
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {"ws_token": "test-secret"})()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        app_module.registry,
        "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted"),
        ),
    )
    client = TestClient(app_module.app)

    assert client.post("/api/command", json={"text": "hello"}).status_code == 401
    assert client.post(
        "/api/command",
        headers={"Authorization": "Bearer wrong"},
        json={"text": "hello"},
    ).status_code == 401
    response = client.post(
        "/api/command",
        headers={"Authorization": "Bearer test-secret"},
        json={"text": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "accepted"


def test_remote_command_fails_closed_without_configured_token(monkeypatch):
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {"ws_token": ""})()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    response = TestClient(app_module.app).post(
        "/api/command", headers={"X-Agent-Token": "anything"}, json={"text": "hello"}
    )
    assert response.status_code == 503


def test_health_dsn_masker_preserves_user_and_hides_password():
    masked = _mask_dsn("postgresql://agentos:super-secret@localhost:5432/agentos")
    assert masked == "postgresql://agentos:***@localhost:5432/agentos"
    assert "super-secret" not in masked


def test_health_dsn_masker_hides_keyword_passwords():
    masked = _mask_dsn("host=localhost dbname=agentos user=agentos password='super secret'")
    assert masked == "host=localhost dbname=agentos user=agentos password=***"
    assert "super secret" not in masked


def test_dashboard_escapes_every_server_derived_html_field():
    html = (Path(__file__).parents[1] / "kernel" / "static" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    for expression in (
        "esc(s.last_channel || \"—\")",
        "esc(live)",
        "esc(i.kind)",
        "esc(i.what)",
        "esc(i.action)",
        "esc(t.channel)",
        "esc(t.text)",
        "esc(t.reply)",
    ):
        assert expression in html
