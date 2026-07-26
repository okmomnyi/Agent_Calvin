"""Kernel tests: skill discovery, graceful degradation for unbuilt skills, voice formatting."""

from __future__ import annotations

import importlib
import types
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


def test_remote_command_requires_a_session_cookie(monkeypatch, mem):
    """Slice 0a: /api/command moved off the shared AGENT_WS_TOKEN onto a real login session.
    No cookie, or a garbage one, is rejected; logging in via the break-glass password issues
    a cookie that the same client then rides to a 200 — proving both halves of 0a's own
    stop-gate ("prove a guarded endpoint rejects without the cookie and accepts with it")."""
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(
        app_module.registry,
        "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted"),
        ),
    )
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    # https base_url: the session cookie is Secure, so httpx's cookie jar (correctly)
    # refuses to send it back over plain http, same as a real browser would.
    client = TestClient(app_module.app, base_url="https://testserver")

    assert client.post("/api/command", json={"text": "hello"}).status_code == 401
    client.cookies.set("agentos_session", "not-a-real-token")
    assert client.post("/api/command", json={"text": "hello"}).status_code == 401
    client.cookies.clear()

    login = client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    assert login.status_code == 200
    assert "agentos_session" in client.cookies

    response = client.post("/api/command", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json()["text"] == "accepted"


def test_password_login_rejects_wrong_password(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app)

    response = client.post("/api/auth/password", json={"password": "wrong password entirely"})
    assert response.status_code == 401
    assert "agentos_session" not in client.cookies


def test_session_cookie_is_httponly_secure_samesite_strict(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    response = client.post("/api/auth/password", json={"password": "correct horse battery staple"})

    set_cookie = response.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_dev_insecure_flag_drops_the_secure_cookie_attribute(mem, monkeypatch):
    """A local Vite dev server on plain HTTP can't carry a Secure cookie at all -- this is
    the one, explicit, loudly-logged escape hatch, and it must never affect httpOnly or
    SameSite, only Secure."""
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {
        "get": lambda self, *keys, default=None: True if keys == ("auth", "dev_insecure") else default,
    })()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    response = client.post("/api/auth/password", json={"password": "correct horse battery staple"})

    set_cookie = response.headers.get("set-cookie", "")
    assert "secure" not in set_cookie.lower()
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_dev_insecure_defaults_off_with_a_bare_settings_object(mem, monkeypatch):
    """A Settings object with no `auth` key configured at all (config.yaml stripped down,
    or a minimal test double) must default to secure, never silently permissive."""
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    response = client.post("/api/auth/password", json={"password": "correct horse battery staple"})

    assert "secure" in response.headers.get("set-cookie", "").lower()


def test_logout_revokes_the_session(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(
        app_module.registry, "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted")))
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    assert client.post("/api/command", json={"text": "hi"}).status_code == 200
    raw_cookie = client.cookies.get("agentos_session")

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200

    # Prove the session is REVOKED server-side, not just that the client forgot the cookie:
    # re-attach the exact same raw value logout deleted and confirm it's still refused.
    client.cookies.set("agentos_session", raw_cookie)
    assert client.post("/api/command", json={"text": "hi"}).status_code == 401, \
        "a revoked session must not keep working even when the raw cookie is replayed"


# ================================================================= WS ticket flow (Slice 0b)
# "This is the bug that started this": a browser cannot set an Authorization header on
# `new WebSocket()`, so the old static AGENT_WS_TOKEN rode in the URL as a long-lived,
# reusable value. A ticket is minted over REST (which CAN carry the session cookie), then
# burned on first use at the WS handshake.
def _logged_in_client(mem, monkeypatch, app_module):
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(
        app_module.registry, "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted")))
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    return client


# ================================================================= job listings (Phase 36)
def test_jobs_requires_a_session(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    assert client.get("/api/jobs").status_code == 401


def test_jobs_lists_drafted_and_notified_sorted_by_score(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("kernel.app.get_memory", lambda: mem)
    client = _logged_in_client(mem, monkeypatch, app_module)

    mem.upsert_job("remoteok", "low", title="Low notified", company="Acme")
    low_id = mem.get_job_by_ref("remoteok", "low")["id"]
    mem.score_job(low_id, 60, category="cloud_devops")
    mem.set_job_status(low_id, "notified")

    mem.upsert_job("remoteok", "high", title="High drafted", company="Acme")
    high_id = mem.get_job_by_ref("remoteok", "high")["id"]
    mem.score_job(high_id, 85, category="cloud_devops")
    mem.set_job_status(high_id, "drafted")

    mem.upsert_job("remoteok", "skipped", title="Skipped one", company="Acme")
    skipped_id = mem.get_job_by_ref("remoteok", "skipped")["id"]
    mem.set_job_status(skipped_id, "skipped")

    response = client.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert [j["id"] for j in body["jobs"]] == [high_id, low_id]
    assert body["total"] == 2


def test_ws_ticket_requires_a_session_cookie(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    assert client.post("/api/auth/ws-ticket").status_code == 401


def test_ws_handshake_with_no_ticket_is_rejected(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    with client.websocket_connect("/ws/voice") as ws:
        ws.send_json({"text": "hello"})
        reply = ws.receive_json()
        assert reply["ok"] is False


def test_ws_handshake_with_a_valid_ticket_is_accepted(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client(mem, monkeypatch, app_module)

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        reply = ws.receive_json()
        assert reply["ok"] is True
        assert reply["text"] == "accepted"


def test_ws_voice_omits_the_directive_key_for_an_unrelated_skill(mem, monkeypatch):
    """core/presenter.py returns None for a skill it doesn't map -- kernel/app.py must
    then send no `directive` key at all, so an older client sees nothing different, and a
    newer Director knows not to touch whatever the stage is already showing."""
    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client(mem, monkeypatch, app_module)

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        reply = ws.receive_json()
        assert reply["ok"] is True
        assert "directive" not in reply


def test_ws_voice_carries_a_stage_directive_for_the_stage_skill(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(
        app_module.registry, "handle_command",
        lambda text, use_llm=True: (
            Intent(name="stage_idle", skill="stage", action="idle", via="keyword"),
            CommandResult(text="Back to idle.", data={"stage_kind": "idle"})))
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "back to idle"})
        reply = ws.receive_json()
        assert reply["directive"]["focus"] is None
        assert reply["directive"]["transition"] == "settle"
        assert reply["directive"]["widgets"] == []


def test_ws_ticket_is_single_use(mem, monkeypatch):
    """The other half of the fix: replaying the URL (browser history, a proxy log) must
    not work a second time."""
    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client(mem, monkeypatch, app_module)

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        ws.receive_json()

    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello again"})
        reply = ws.receive_json()
        assert reply["ok"] is False


def test_ws_ticket_expires(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    now = [1_000_000.0]
    store = get_store(mem)
    store._now = lambda: now[0]
    monkeypatch.setattr("core.auth.get_store", lambda *a, **k: store)
    monkeypatch.setattr(
        app_module.registry, "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted")))

    store.set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]

    now[0] += 20   # past WS_TICKET_TTL (15s)
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        reply = ws.receive_json()
        assert reply["ok"] is False


def test_ws_drops_the_connection_if_the_session_is_revoked_mid_stream(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client(mem, monkeypatch, app_module)
    from core.auth import get_store

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    raw_cookie = client.cookies.get("agentos_session")

    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        first = ws.receive_json()
        assert first["ok"] is True

        get_store(mem).revoke_session_by_token(raw_cookie)

        ws.send_json({"text": "still there?"})
        second = ws.receive_json()
        assert second["ok"] is False


def test_laptop_voice_client_connects_via_a_device_credential_not_a_shared_token(
    mem, monkeypatch,
):
    """Slice 0e: the laptop client (client/voice_client.py) has no browser and so no
    WebAuthn ceremony — it authenticates with a long-lived, explicitly-issued, revocable
    device credential instead of the old shared AGENT_WS_TOKEN, riding as a plain Cookie
    header (nothing browser-specific about a cookie — any HTTP client can set one) to mint
    the same single-use WS ticket the dashboard uses."""
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(
        app_module.registry, "handle_command",
        lambda text, use_llm=True: (
            Intent(name="test", skill="chat", action="reply", via="keyword"),
            CommandResult(text="accepted")))
    from core.auth import get_store

    device_token = get_store(mem).issue_device_credential("Test Laptop")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.cookies.set("agentos_session", device_token)

    ticket = client.post("/api/auth/ws-ticket").json()["ticket"]
    with client.websocket_connect(f"/ws/voice?ticket={ticket}") as ws:
        ws.send_json({"text": "hello"})
        reply = ws.receive_json()
        assert reply["ok"] is True
        assert reply["text"] == "accepted"


def test_revoked_device_credential_cannot_mint_a_new_ws_ticket(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    store = get_store(mem)
    device_token = store.issue_device_credential("Stolen Laptop")
    store.revoke_session_by_token(device_token)

    client = TestClient(app_module.app, base_url="https://testserver")
    client.cookies.set("agentos_session", device_token)
    assert client.post("/api/auth/ws-ticket").status_code == 401


# ================================================================= WebAuthn endpoints (Slice 0c)
# The ceremony logic itself (challenge single-use, sign_count regression) is exercised
# thoroughly in tests/test_auth.py against injected fake crypto. What matters here is the
# REST wiring: the bootstrap gate, and that a bad/missing credential fails cleanly (401/400)
# rather than crashing — a real successful verify needs an actual authenticator + browser,
# which pytest doesn't have.
def test_register_options_from_a_non_localhost_origin_is_refused_when_nothing_is_set_up(
    mem, monkeypatch,
):
    """The bootstrap guard: nothing configured yet (no password, no passkey) means only
    localhost / the droplet console may register the first credential."""
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(app_module, "_is_localhost", lambda request: False)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/webauthn/register/options", json={"label": "Laptop"})
    assert response.status_code == 403


def test_register_options_from_localhost_succeeds_when_nothing_is_set_up(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(app_module, "_is_localhost", lambda request: True)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/webauthn/register/options", json={"label": "Laptop"})
    assert response.status_code == 200
    assert "challenge" in response.json()


def test_register_options_once_a_password_exists_requires_a_session(mem, monkeypatch):
    """Adding a device once something already exists is an ordinary logged-in action, not
    the bootstrap path — localhost is no longer special, a session is required instead."""
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(app_module, "_is_localhost", lambda request: True)  # even from "localhost"
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")

    assert client.post(
        "/api/auth/webauthn/register/options", json={"label": "Laptop"}).status_code == 401

    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    assert client.post(
        "/api/auth/webauthn/register/options", json={"label": "Laptop"}).status_code == 200


def test_register_verify_from_a_non_localhost_origin_is_refused_when_nothing_is_set_up(
    mem, monkeypatch,
):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(app_module, "_is_localhost", lambda request: False)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/webauthn/register/verify",
                           json={"label": "Laptop", "credential": {}})
    assert response.status_code == 403


def test_register_verify_with_a_bad_credential_fails_cleanly_not_a_crash(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    monkeypatch.setattr(app_module, "_is_localhost", lambda request: True)
    client = TestClient(app_module.app, base_url="https://testserver")

    # No prior /register/options call, so there is no pending challenge -- this is exactly
    # the shape of request an attacker replaying an old page would send.
    response = client.post("/api/auth/webauthn/register/verify",
                           json={"label": "Laptop", "credential": {}})
    assert response.status_code == 400


def test_login_options_with_no_passkey_registered_returns_400_not_a_dead_end(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/webauthn/login/options")
    assert response.status_code == 400   # caller falls back to the break-glass password


def test_login_verify_with_a_bad_credential_is_401_not_a_crash(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/webauthn/login/verify", json={"credential": {}})
    assert response.status_code == 401


# ================================================================= recovery + lockout (0d)
def test_recovery_login_succeeds_and_sets_a_session_cookie(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    code = get_store(mem).generate_recovery_codes(1)[0]
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/auth/recovery", json={"code": code})
    assert response.status_code == 200
    assert "agentos_session" in client.cookies
    assert "register a fresh passkey" in response.json()["notice"].lower()


def test_recovery_code_is_single_use_over_rest_too(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    code = get_store(mem).generate_recovery_codes(1)[0]
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/recovery", json={"code": code})
    client.cookies.clear()

    second = client.post("/api/auth/recovery", json={"code": code})
    assert second.status_code == 401


def test_password_login_locks_out_after_repeated_failures(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")

    for _ in range(5):   # password's lockout threshold
        client.post("/api/auth/password", json={"password": "wrong"})

    # Even the CORRECT password is refused while locked out.
    response = client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers


# ================================================================= high-tier step-up (0d)
def _logged_in_client_with_password(mem, monkeypatch, app_module):
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    # /api/approvals/*/resolve reads core.approvals.ApprovalStore, which resolves its own
    # memory the same lazy way core.auth does -- both need pointing at the test schema.
    monkeypatch.setattr("core.approvals.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    return client


def test_low_tier_approval_resolves_with_no_step_up_required(mem, monkeypatch):
    from core.approvals import TIER_LOW, get_store as get_approval_store

    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client_with_password(mem, monkeypatch, app_module)
    action_id, _ = get_approval_store(mem).propose(
        "email_trash", "Trash a newsletter", tier=TIER_LOW, permission_key="email_trash:x")

    response = client.post(f"/api/approvals/{action_id}/resolve", json={"approve": True})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_high_tier_approval_with_no_passkey_registered_falls_back_to_ordinary_approval(
    mem, monkeypatch,
):
    """"falls back to the normal approval confirmation if no passkey is available so he is
    never locked out of his own gate" -- the brief's own words."""
    from core.approvals import TIER_HIGH, get_store as get_approval_store

    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client_with_password(mem, monkeypatch, app_module)
    action_id, _ = get_approval_store(mem).propose(
        "job_apply", "Apply to Acme", tier=TIER_HIGH, permission_key="job_apply:acme")

    response = client.post(f"/api/approvals/{action_id}/resolve", json={"approve": True})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_high_tier_approval_with_a_passkey_registered_demands_step_up(mem, monkeypatch):
    from core.approvals import TIER_HIGH, get_store as get_approval_store
    from core.auth import get_store as get_auth_store

    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client_with_password(mem, monkeypatch, app_module)

    def _fake_options(**_):
        return types.SimpleNamespace(challenge=b"chal")

    get_auth_store(mem).start_registration(
        rp_id="localhost", user_name="calvin", generate_fn=_fake_options)
    get_auth_store(mem).verify_registration(
        {}, expected_rp_id="localhost", expected_origin="https://localhost", label="Laptop",
        verify_fn=lambda **_: types.SimpleNamespace(
            credential_id=b"cred-1", credential_public_key=b"pk", sign_count=0))

    action_id, _ = get_approval_store(mem).propose(
        "job_apply", "Apply to Acme", tier=TIER_HIGH, permission_key="job_apply:acme")

    # No step_up_credential supplied at all -> refused before resolve() ever runs.
    response = client.post(f"/api/approvals/{action_id}/resolve", json={"approve": True})
    assert response.status_code == 401
    assert get_approval_store(mem).get(action_id).status == "pending", \
        "a high-tier action must stay pending, not silently approved, when step-up fails"


def test_high_tier_denial_never_requires_step_up(mem, monkeypatch):
    """Refusing something never needs proof of presence -- only an approval does."""
    from core.approvals import TIER_HIGH, get_store as get_approval_store
    from core.auth import get_store as get_auth_store

    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client_with_password(mem, monkeypatch, app_module)

    def _fake_options(**_):
        return types.SimpleNamespace(challenge=b"chal")

    get_auth_store(mem).start_registration(
        rp_id="localhost", user_name="calvin", generate_fn=_fake_options)
    get_auth_store(mem).verify_registration(
        {}, expected_rp_id="localhost", expected_origin="https://localhost", label="Laptop",
        verify_fn=lambda **_: types.SimpleNamespace(
            credential_id=b"cred-1", credential_public_key=b"pk", sign_count=0))

    action_id, _ = get_approval_store(mem).propose(
        "job_apply", "Apply to Acme", tier=TIER_HIGH, permission_key="job_apply:acme")

    response = client.post(f"/api/approvals/{action_id}/resolve", json={"approve": False})
    assert response.status_code == 200
    assert response.json()["status"] == "denied"


def test_resolving_an_unknown_action_id_is_404(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    client = _logged_in_client_with_password(mem, monkeypatch, app_module)

    response = client.post("/api/approvals/999999/resolve", json={"approve": True})
    assert response.status_code == 404


def test_approvals_resolve_requires_a_session(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.post("/api/approvals/1/resolve", json={"approve": True})
    assert response.status_code == 401


def test_health_dsn_masker_preserves_user_and_hides_password():
    masked = _mask_dsn("postgresql://agentos:super-secret@localhost:5432/agentos")
    assert masked == "postgresql://agentos:***@localhost:5432/agentos"
    assert "super-secret" not in masked


def test_health_dsn_masker_hides_keyword_passwords():
    masked = _mask_dsn("host=localhost dbname=agentos user=agentos password='super secret'")
    assert masked == "host=localhost dbname=agentos user=agentos password=***"
    assert "super secret" not in masked


def test_dashboard_never_assigns_innerhtml_from_server_derived_data():
    """The HUD (frontend/, served at /dashboard) renders session/turn/approval text as plain
    React children, never raw HTML — so there is no escaping discipline to maintain (and none
    to forget) for fields like session.last_channel, a turn's text/reply, or an approval's
    kind/what/action, all of which round-trip through Calvin's own input.

    Rebuilt on React (superseding the old vanilla-JS dashboard, which enforced this via
    `textContent` never `innerHTML`): the equivalent risk in JSX is `dangerouslySetInnerHTML`,
    React's own escape hatch back to raw HTML. Neither that nor a raw DOM `.innerHTML =`
    assignment (a component could still reach for the DOM directly) may appear anywhere
    under frontend/src.
    """
    frontend_src = Path(__file__).parents[1] / "frontend" / "src"
    offenders = []
    for path in list(frontend_src.rglob("*.ts")) + list(frontend_src.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        if "dangerouslySetInnerHTML" in text or ".innerHTML" in text:
            offenders.append(str(path.relative_to(frontend_src)))
    assert not offenders, f"raw-HTML rendering found in: {offenders}"


def test_health_gives_liveness_publicly_but_detail_only_to_a_session(mem, monkeypatch):
    """An uptime probe needs 200 + ok/degraded. It does not need the deployment's inventory.

    /api/health used to serve the full skill list, timezone, queue depth and which
    credentials were configured to anyone who asked -- on the same droplet whose weekly
    report-only recon scan exists to find exactly this kind of exposure (§0 P12). The split
    keeps the container HEALTHCHECK working without logging in, because requiring that there
    would mean shipping a credential to every probe. Migrated off AGENT_WS_TOKEN in 0e —
    the detail branch now requires a real session, same as every other guarded /api/* route.
    """
    app_module = importlib.import_module("kernel.app")
    settings = type("Settings", (), {
        "nvidia_api_key": "k", "telegram_bot_token": "t",
        "telegram_chat_id": "c", "tz": "Africa/Nairobi"})()
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")

    public = client.get("/api/health")
    assert public.status_code == 200                  # the HEALTHCHECK must still pass
    body = public.json()
    assert set(body) == {"status", "scheduler_running", "db_ok"}
    for leaked in ("skills", "timezone", "queue", "gmail_token", "nim_key_present"):
        assert leaked not in body, f"/api/health disclosed {leaked} to an unauthenticated caller"

    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    detailed = client.get("/api/health")
    assert detailed.status_code == 200
    assert "skills" in detailed.json() and "queue" in detailed.json()


def test_the_voice_endpoint_is_not_public(mem, monkeypatch):
    app_module = importlib.import_module("kernel.app")
    monkeypatch.setattr("core.auth.get_memory", lambda: mem)
    from core.auth import get_store

    get_store(mem).set_password("correct horse battery staple")
    client = TestClient(app_module.app, base_url="https://testserver")

    assert client.get("/api/voice").status_code == 401
    client.post("/api/auth/password", json={"password": "correct horse battery staple"})
    assert client.get("/api/voice").status_code == 200


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
