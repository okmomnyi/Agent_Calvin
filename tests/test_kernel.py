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


def test_health_gives_liveness_publicly_but_detail_only_to_the_token(monkeypatch):
    """An uptime probe needs 200 + ok/degraded. It does not need the deployment's inventory.

    /api/health used to serve the full skill list, timezone, queue depth and which
    credentials were configured to anyone who asked -- on the same droplet whose weekly
    report-only recon scan exists to find exactly this kind of exposure (§0 P12). The split
    keeps the container HEALTHCHECK working without a token, because requiring one there
    would mean shipping the secret to every probe.
    """
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {
        "ws_token": "test-secret", "nvidia_api_key": "k", "telegram_bot_token": "t",
        "telegram_chat_id": "c", "tz": "Africa/Nairobi"})()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    client = TestClient(app_module.app)

    public = client.get("/api/health")
    assert public.status_code == 200                  # the HEALTHCHECK must still pass
    body = public.json()
    assert set(body) == {"status", "scheduler_running", "db_ok"}
    for leaked in ("skills", "timezone", "queue", "gmail_token", "nim_key_present"):
        assert leaked not in body, f"/api/health disclosed {leaked} to an unauthenticated caller"

    detailed = client.get("/api/health", headers={"Authorization": "Bearer test-secret"})
    assert detailed.status_code == 200
    assert "skills" in detailed.json() and "queue" in detailed.json()


def test_the_voice_endpoint_is_not_public(monkeypatch):
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {"ws_token": "test-secret"})()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    client = TestClient(app_module.app)

    assert client.get("/api/voice").status_code == 401
    assert client.get("/api/voice", headers={"Authorization": "Bearer test-secret"}).status_code == 200


def test_config_has_no_key_that_nothing_reads():
    """A settings key read by no code is worse than a missing one.

    `jobs.skip_unpaid: true` and `events.free_only: true` both sat in config.yaml looking
    load-bearing and were referenced nowhere. Each named a real behaviour that happens to be
    hardcoded, so the values were accidentally right -- which is exactly why nobody noticed.
    Flip either to `false` and nothing would have changed, silently.

    Keys consumed by iteration (feed names, flip categories, voice aliases) are legitimate
    data rather than settings, so only their PARENT needs to be read.
    """
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

    sources = [(root / "manage.py").read_text(encoding="utf-8")]
    for package in ("core", "skills", "kernel", "client"):
        sources += [p.read_text(encoding="utf-8", errors="ignore")
                    for p in (root / package).rglob("*.py")]
    blob = "\n".join(sources)

    # Containers whose children are data, not settings: reading the parent is enough.
    DATA_CONTAINERS = {"rss_feeds", "category_velocity_days", "registry", "routes",
                       "transcription_portals", "commitments", "feeds", "targets",
                       "apps", "collaborations", "sources", "interest_tags"}

    def walk(node, path=()):
        if isinstance(node, dict):
            for key, value in node.items():
                yield path + (str(key),), value
                if str(key) not in DATA_CONTAINERS:
                    yield from walk(value, path + (str(key),))

    dead = [".".join(path) for path, _ in walk(config)
            if f'"{path[-1]}"' not in blob and f"'{path[-1]}'" not in blob]
    assert not dead, f"config keys nothing reads (they imply a switch that isn't wired): {dead}"
