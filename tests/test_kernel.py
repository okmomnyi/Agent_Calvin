"""Kernel tests: skill discovery, graceful degradation for unbuilt skills, voice formatting."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.intent import Intent, IntentRouter
from core.skill import CommandResult
from kernel.app import _client_actions, to_spoken
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


def test_dashboard_never_assigns_innerhtml_from_server_derived_data():
    """Phase 36's HUD (frontend/, served at /dashboard) renders session/turn/approval text via
    `textContent`, never `innerHTML` string interpolation — so there is no escaping discipline
    to maintain (and none to forget) for fields like session.last_channel, a turn's text/reply,
    or an approval's kind/what/action, all of which round-trip through Calvin's own input.
    `.innerHTML =` never appearing under frontend/src is the structural guarantee; the old
    dashboard's per-field `esc(...)` wrapping is superseded, not replicated.
    """
    frontend_src = Path(__file__).parents[1] / "frontend" / "src"
    offenders = [
        str(path.relative_to(frontend_src))
        for path in frontend_src.rglob("*.js")
        if "innerHTML" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"innerHTML assignment found in: {offenders}"


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


# ==================================================== _client_actions narrow waist (Phase 36)
def _result(data: dict) -> CommandResult:
    return CommandResult(text="ok", data=data)


def test_client_actions_passes_through_a_valid_open_url():
    out = _client_actions(_result({"client_actions": [
        {"op": "open_url", "url": "https://example.com"}]}))
    assert out == [{"op": "open_url", "url": "https://example.com"}]


def test_client_actions_drops_a_file_url_even_if_a_buggy_skill_emits_one():
    """The skill layer already rejects this (skills/web_open.py); this asserts the SERVER
    still refuses it even if that first line of defence is ever bypassed by a bug."""
    out = _client_actions(_result({"client_actions": [
        {"op": "open_url", "url": "file:///etc/passwd"}]}))
    assert out == []


def test_client_actions_drops_a_javascript_url():
    out = _client_actions(_result({"client_actions": [
        {"op": "open_url", "url": "javascript:alert(1)"}]}))
    assert out == []


def test_client_actions_still_passes_through_a_valid_app_op():
    out = _client_actions(_result({"client_actions": [{"op": "open", "app": "spotify"}]}))
    assert out == [{"op": "open", "app": "spotify"}]


def test_client_actions_drops_an_unrecognized_op():
    out = _client_actions(_result({"client_actions": [{"op": "delete_everything", "app": "x"}]}))
    assert out == []


def test_client_actions_ignores_extra_keys_on_a_valid_action():
    """Only the fields each op actually needs reach the wire — a stray key (a path, an
    argv) can't be smuggled through even by a buggy skill."""
    out = _client_actions(_result({"client_actions": [
        {"op": "open_url", "url": "https://example.com", "argv": ["rm", "-rf", "/"]}]}))
    assert out == [{"op": "open_url", "url": "https://example.com"}]


# ------------------------------------------------------------------- phone ops (Phase 36)
def test_client_actions_passes_through_a_valid_e164_call():
    out = _client_actions(_result({"client_actions": [
        {"op": "call", "number": "+254712345678"}]}))
    assert out == [{"op": "call", "number": "+254712345678"}]


@pytest.mark.parametrize("number", [
    "0712345678",           # not E.164
    "+254712345678; ls",    # injection attempt
    "'; rm -rf /",
    "",
])
def test_client_actions_drops_a_malformed_or_non_e164_number(number):
    """A malformed or non-E.164 number must never reach a client action — this is the
    server-side half of the same guarantee client/adb_bridge.py enforces laptop-side."""
    out = _client_actions(_result({"client_actions": [{"op": "call", "number": number}]}))
    assert out == []


def test_client_actions_passes_through_answer_and_hangup():
    out = _client_actions(_result({"client_actions": [{"op": "answer"}, {"op": "hangup"}]}))
    assert out == [{"op": "answer"}, {"op": "hangup"}]


# ==================================================== sticky sessions yield (regression)
# Telegram log: a code_tutor session swallowed "create a playlist for late night coding" and
# every message after it for two days, because _active_continuation() had no escape at all --
# any active session key intercepted everything unconditionally, before the keyword router
# ever ran. Fixed: a high-confidence (0.9) intent for a DIFFERENT skill now ends the stale
# session instead of being swallowed by it.
def test_sticky_session_yields_to_a_high_confidence_different_skill_intent(mem, monkeypatch, fake_llm):
    monkeypatch.setattr("core.memory.get_memory", lambda: mem)
    mem.kv_set("code_tutor.session", '{"mode": "explain", "topic": "pointers"}')

    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    intent, result = reg.handle_command("create me a late night coding playlists")

    assert intent.skill == "music" and intent.action == "playlist"
    assert mem.kv_get("code_tutor.session") in (None, ""), \
        "the stale session must be cleared, not left pending, once it yields"


def test_sticky_session_still_owns_a_message_with_no_high_confidence_match(mem, monkeypatch, fake_llm):
    """The escape only fires for a clear, differently-routed intent -- an ordinary tutor
    answer ("a hash table maps keys to values") must still reach the tutor, not fall through."""
    monkeypatch.setattr("core.memory.get_memory", lambda: mem)
    mem.kv_set("code_tutor.session", '{"mode": "explain", "topic": "pointers"}')

    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    intent, result = reg.handle_command("a hash table maps keys to values")

    assert intent.skill == "code_tutor" and intent.action == "continue"
    assert mem.kv_get("code_tutor.session"), "an ordinary answer must not clear the session"


def test_sticky_session_does_not_yield_to_its_own_skill(mem, monkeypatch, fake_llm):
    """A high-confidence match for the SAME skill that owns the session isn't a foreign
    interruption -- it should still go through the session's own continuation, not be treated
    as an escape (there is nothing to escape TO)."""
    monkeypatch.setattr("core.memory.get_memory", lambda: mem)
    mem.kv_set("spaced_rep.session", '{"mode": "quiz"}')

    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    intent, result = reg.handle_command("quiz me on kubernetes")

    assert intent.skill == "spaced_rep" and intent.action == "quiz_answer"
    assert mem.kv_get("spaced_rep.session"), \
        "same-skill traffic must stay in the continuation, not be treated as foreign"
