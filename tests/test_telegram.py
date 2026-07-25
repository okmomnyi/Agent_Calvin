"""Telegram BotCore tests: authorization, command routing, inline callbacks, job buttons,
mock continuation, status snapshot, voice-note transcription. No network / no PTB runtime."""

from __future__ import annotations

import time

import pytest

import types

from skills.telegram_bot import BotCore, job_buttons, parse_callback


class _FakeRegistry:
    def __init__(self):
        self.calls: list = []
        self.skills = {"persona": 1, "job_hunter": 1, "voice": 1, "research": 1}

    def dispatch_intent(self, intent):
        self.calls.append((intent.skill, intent.action, dict(intent.args)))
        return types.SimpleNamespace(text=f"dispatched:{intent.skill}.{intent.action}", ok=True)

    def handle_command(self, text):
        self.calls.append(("__route__", text))
        return types.SimpleNamespace(name="x"), types.SimpleNamespace(text=f"routed:{text}", ok=True)


def _core(mem, chat_id="42"):
    core = BotCore(registry=_FakeRegistry(), memory=mem)
    core.settings = types.SimpleNamespace(telegram_chat_id=chat_id, telegram_bot_token="x")
    return core


# ------------------------------------------------------------------ auth
def test_authorization(mem):
    core = _core(mem, chat_id="42")
    assert core.is_authorized(42) is True
    assert core.is_authorized("42") is True
    assert core.is_authorized(999) is False


def test_unauthorized_when_no_chat_configured(mem):
    core = _core(mem, chat_id="")
    assert core.is_authorized(42) is False


# ------------------------------------------------------------------ pure helpers
def test_parse_callback():
    assert parse_callback("j:apply:123") == ("apply", 123)
    assert parse_callback("j:skip:7") == ("skip", 7)
    assert parse_callback("garbage") == (None, None)
    assert parse_callback("j:apply:notanint") == (None, None)


def test_job_buttons_shape():
    rows = job_buttons([{"id": 5}])
    labels = dict(rows[0])
    assert labels["✅ Apply"] == "j:apply:5"
    assert labels["📄 Tailor CV"] == "j:tailor:5"
    assert labels["⏭ Skip"] == "j:skip:5"
    assert labels["🔎 Details"] == "j:details:5"


# ------------------------------------------------------------------ command routing
def test_help_and_status(mem):
    core = _core(mem)
    assert "AgentOS remote control" in core.run_command("help")
    assert "AgentOS status" in core.run_command("status")


def test_run_command_maps_to_skill(mem):
    core = _core(mem)
    core.run_command("ask", "what tools do I use")
    core.run_command("find", "kubernetes")
    core.run_command("prep", "Acme")
    skills_actions = [(c[0], c[1]) for c in core.registry.calls if c[0] != "__route__"]
    assert ("persona", "answer") in skills_actions
    assert ("research", "search") in skills_actions
    assert ("interview_prep", "prep") in skills_actions


def test_approve_parses_ids(mem):
    core = _core(mem)
    core.run_command("approve", "1, 3 5")
    call = next(c for c in core.registry.calls if c[0] == "job_hunter")
    assert call[2]["selection"] == [1, 3, 5]


def test_voiceoff_voiceon_route_to_voice_skill(mem):
    core = _core(mem)
    core.run_command("voiceoff")
    core.run_command("voiceon")
    actions = [(c[0], c[1]) for c in core.registry.calls if c[0] == "voice"]
    assert ("voice", "mute") in actions
    assert ("voice", "unmute") in actions


# ------------------------------------------------------------------ approval namespace (regression)
# Telegram log: a job digest says "Reply `approve 6229,6234,6235`" (JOB ids); the reply
# instead hit "No pending action #6229" -- _try_approval_reply() checks core.approvals'
# pending_actions table (a totally different id sequence, owned by proactive.py) and used to
# dead-end there instead of falling through to the router that would resolve job ids
# correctly.
def test_approve_reply_falls_through_to_the_router_when_the_id_is_not_a_pending_action(mem):
    from core.approvals import get_store

    core = _core(mem)
    # A real pending action exists (so _try_approval_reply doesn't bail out early on "no
    # pending actions at all") but its id has nothing to do with the job digest's ids.
    get_store(mem).propose("email_trash", "trash newsletter", tier="low",
                            permission_key="email_trash:test")
    result = core.route_text("approve 6229,6234,6235")
    assert result == "routed:approve 6229,6234,6235"
    assert ("__route__", "approve 6229,6234,6235") in core.registry.calls


def test_approve_reply_still_resolves_a_real_pending_action(mem):
    """The fallthrough must not break the case it was built for."""
    from core.approvals import get_store

    core = _core(mem)
    action_id, status = get_store(mem).propose(
        "email_trash", "trash newsletter", tier="low", permission_key="email_trash:test")
    assert status == "pending"
    result = core.route_text(f"{action_id} yes")
    assert "Approved" in result
    assert not any(c[0] == "__route__" for c in core.registry.calls)


# ------------------------------------------------------------------ free text + mock
def test_route_text_uses_intent_engine(mem):
    core = _core(mem)
    assert core.route_text("any new jobs?") == "routed:any new jobs?"


def test_route_text_continues_active_mock(mem):
    core = _core(mem)
    mem.kv_set("interview_prep.mock", '{"company":"Acme","questions":["q"],"idx":0,"scores":[]}')
    core.route_text("here is my answer")
    call = core.registry.calls[-1]
    assert call[0] == "interview_prep" and call[1] == "mock_answer"
    assert call[2]["answer"] == "here is my answer"


# ------------------------------------------------------------------ callbacks
def test_callback_apply_dispatches_approve(mem):
    core = _core(mem)
    core.handle_callback("j:apply:9")
    call = core.registry.calls[-1]
    assert call == ("job_hunter", "approve", {"selection": [9]})


def test_callback_skip_sets_status(mem):
    core = _core(mem)
    mem.upsert_job("remoteok", "x1", title="DevOps", company="Acme")
    jid = mem.get_job_by_ref("remoteok", "x1")["id"]
    msg = core.handle_callback(f"j:skip:{jid}")
    assert "Skipped" in msg
    assert mem.get_job(jid)["status"] == "skipped"


def test_callback_quiz_grade_routes_to_spaced_rep(mem):
    core = _core(mem)
    core.handle_callback("q:grade:good")
    assert core.registry.calls[-1] == ("spaced_rep", "grade", {"grade": "good"})
    core.handle_callback("q:reveal")
    assert core.registry.calls[-1] == ("spaced_rep", "reveal", {})


def test_callback_candidate_approve_reject(mem):
    core = _core(mem)
    core.handle_callback("c:approve:7")
    assert core.registry.calls[-1] == ("spaced_rep", "approve_card", {"card_id": 7})
    core.handle_callback("c:reject:7")
    assert core.registry.calls[-1] == ("spaced_rep", "reject_card", {"card_id": 7})


def test_route_text_continues_active_quiz(mem):
    core = _core(mem)
    mem.kv_set("spaced_rep.session", '{"unit":"CS","ids":[1],"idx":0}')
    core.route_text("my spoken answer")
    assert core.registry.calls[-1] == ("spaced_rep", "quiz_answer", {"answer": "my spoken answer"})


def test_callback_details_returns_job(mem):
    core = _core(mem)
    mem.upsert_job("remoteok", "x2", title="SRE", company="Nimbus")
    jid = mem.get_job_by_ref("remoteok", "x2")["id"]
    mem.save_cover(jid, apply_kind="email", apply_target="jobs@nimbus.com", cover_text="Hi there")
    msg = core.handle_callback(f"j:details:{jid}")
    assert "SRE" in msg and "Nimbus" in msg and "Hi there" in msg


# ------------------------------------------------------------------ jobs payload
def test_jobs_payload_lists_awaiting(mem):
    core = _core(mem)
    mem.upsert_job("remoteok", "a", title="DevOps", company="Acme")
    jid = mem.get_job_by_ref("remoteok", "a")["id"]
    mem.set_job_status(jid, "notified")
    header, jobs = core.jobs_payload()
    assert len(jobs) == 1 and jobs[0]["id"] == jid
    assert "awaiting" in header.lower()


def test_jobs_payload_sorts_across_statuses_by_score_not_status_group(mem):
    """Regression: jobs_by_status() sorts WITHIN one status, but the old code concatenated
    "notified" jobs then "drafted" jobs without re-sorting the combined list -- an 85-scored
    draft could land behind a 60-scored notified job just because of which status it was in.
    Telegram log: "16 of 28 awaiting your call" with 85s at positions 1, 7 and 8."""
    core = _core(mem)
    mem.upsert_job("remoteok", "low", title="Low notified", company="Acme")
    low_id = mem.get_job_by_ref("remoteok", "low")["id"]
    mem.score_job(low_id, 60, category="cloud_devops")
    mem.set_job_status(low_id, "notified")

    mem.upsert_job("remoteok", "high", title="High drafted", company="Acme")
    high_id = mem.get_job_by_ref("remoteok", "high")["id"]
    mem.score_job(high_id, 85, category="cloud_devops")
    mem.set_job_status(high_id, "drafted")

    header, jobs = core.jobs_payload()
    assert [j["id"] for j in jobs] == [high_id, low_id], \
        "the 85-scored drafted job must be listed ahead of the 60-scored notified job"


def test_jobs_payload_empty(mem):
    core = _core(mem)
    header, jobs = core.jobs_payload()
    assert jobs == []


# ------------------------------------------------------------------ voice notes
def test_transcribe_injection(mem):
    core = BotCore(registry=_FakeRegistry(), memory=mem, transcribe=lambda p: "check my email")
    assert core.transcribe("/tmp/x.ogg") == "check my email"


# ============================================================ progress feedback
def test_slow_work_is_acknowledged_before_it_starts(mem):
    """Calvin: "i need to see clearing emails in progress ... to know its already on it".

    A long task with no acknowledgement is indistinguishable from a dead bot.
    """
    core = BotCore(memory=mem)
    assert "email" in core.progress_line("delete all my LinkedIn emails").lower()
    assert core.progress_line("create a playlist for late night coding")


def test_instant_replies_are_not_acknowledged(mem):
    """Acking something that answers immediately is just noise."""
    core = BotCore(memory=mem)
    assert core.progress_line("what time is it") == ""
    assert core.progress_line("/status") == ""


def test_a_broken_ack_never_blocks_the_message(mem, monkeypatch):
    core = BotCore(memory=mem)

    def boom(*a, **k):
        raise RuntimeError("router down")

    monkeypatch.setattr(core, "_PROGRESS_PATTERNS", boom)   # not iterable -> would raise
    assert core.progress_line("anything") == ""      # reported as no-ack, not raised


def test_a_forgotten_session_stops_hijacking_after_the_ttl(mem):
    """The bug Calvin hit: a two-day-old /tutor session ate every message he sent."""
    import json

    from skills.telegram_bot import _LIVE_SESSION_TTL, _TUTOR_KEY

    core = BotCore(memory=mem)
    stale = time.time() - (_LIVE_SESSION_TTL + 60)
    mem.kv_set(_TUTOR_KEY, json.dumps({"mode": "explain", "created_at": stale}))
    assert core._session_fresh(_TUTOR_KEY, ttl=_LIVE_SESSION_TTL) is False
    assert mem.kv_get(_TUTOR_KEY) == "", "the stale session was not cleared"


def test_an_undated_session_is_aged_from_first_sight_not_killed(mem):
    """Killing undated sessions on sight would break a live drill mid-answer."""
    import json

    from skills.telegram_bot import _LIVE_SESSION_TTL, _TUTOR_KEY

    core = BotCore(memory=mem)
    mem.kv_set(_TUTOR_KEY, json.dumps({"mode": "drill"}))     # no created_at
    assert core._session_fresh(_TUTOR_KEY, ttl=_LIVE_SESSION_TTL) is True
    assert json.loads(mem.kv_get(_TUTOR_KEY)).get("created_at"), "it was not stamped"


# ============================================================ one-shot sessions
def test_a_session_ends_after_one_exchange(mem, monkeypatch):
    """Calvin: "the session ends as soon as the response is sent".

    A sticky session is a MODE, and a mode you forgot you were in rewrites the meaning of
    everything you say next -- his /tutor ran two days and turned an email request into an
    smtplib tutorial.
    """
    import json

    from skills.telegram_bot import _TUTOR_KEY

    core = BotCore(memory=mem)
    monkeypatch.setattr(core, "_dispatch", lambda *a, **k: "tutor answered")
    mem.kv_set(_TUTOR_KEY, json.dumps({"mode": "drill", "created_at": time.time()}))

    out = core.route_text("my answer is a linked list")
    assert "tutor answered" in out
    assert "Session closed" in out
    assert mem.kv_get(_TUTOR_KEY) == "", "the session stayed latched"


def test_the_next_message_routes_fresh(mem, monkeypatch):
    """The whole point: a finished drill must not reinterpret the next request."""
    import json

    from skills.telegram_bot import _TUTOR_KEY

    core = BotCore(memory=mem)
    monkeypatch.setattr(core, "_dispatch", lambda *a, **k: "tutor answered")
    mem.kv_set(_TUTOR_KEY, json.dumps({"mode": "drill", "created_at": time.time()}))
    core.route_text("my answer")                       # consumes the session

    routed = {}
    monkeypatch.setattr(core.registry, "handle_command",
                        lambda t: (routed.setdefault("text", t),
                                   type("I", (), {"skill": "music"})(),
                                   type("R", (), {"text": "playlist built"})())[1:])
    out = core.route_text("create a playlist for coding")
    assert "playlist built" in out, "a closed session still hijacked the next message"


def test_a_crashing_skill_still_ends_the_session(mem, monkeypatch):
    """Cleared BEFORE dispatch: a crash that leaves the mode latched is how this started."""
    import json

    from skills.telegram_bot import _TUTOR_KEY

    core = BotCore(memory=mem)

    def boom(*a, **k):
        raise RuntimeError("tutor exploded")

    monkeypatch.setattr(core, "_dispatch", boom)
    mem.kv_set(_TUTOR_KEY, json.dumps({"mode": "drill", "created_at": time.time()}))
    with pytest.raises(RuntimeError):
        core.route_text("my answer")
    assert mem.kv_get(_TUTOR_KEY) == "", "a crash left the session latched"


def test_email_confirmations_are_not_one_shot(mem, monkeypatch):
    """send/trash previews are a two-step CONFIRMATION -- forgetting mid-flow would be worse."""
    import json

    from skills.telegram_bot import _SEND_KEY

    core = BotCore(memory=mem)
    monkeypatch.setattr(core, "_dispatch", lambda *a, **k: "sent")
    mem.kv_set(_SEND_KEY, json.dumps({"to": "x@y.com", "created_at": time.time()}))
    out = core.route_text("confirm send")
    assert "Session closed" not in out


# ------------------------------------------------------------------ password reset (Phase 36)
class _FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []

    def send_email(self, *, to, subject, body, attachments=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": "fake"}


class _SettingsWithResetEmail:
    def __init__(self, chat_id="42", email="okmomanyi@gmail.com"):
        self.telegram_chat_id = chat_id
        self.telegram_bot_token = "x"
        self._email = email

    def get(self, *keys, default=None):
        if keys == ("auth", "password_reset_email"):
            return self._email
        return default


def _core_with_mailer(mem, chat_id="42", email="okmomanyi@gmail.com"):
    mailer = _FakeMailer()
    core = BotCore(registry=_FakeRegistry(), memory=mem, mailer=mailer)
    core.settings = _SettingsWithResetEmail(chat_id=chat_id, email=email)
    return core, mailer


def _extract_code(body: str) -> str:
    import re as _re

    return _re.search(r"code is (\d{6})", body).group(1)


def _extract_password(reply: str) -> str:
    import re as _re

    return _re.search(r"New password:\n\n(\S+)\n\n", reply).group(1)


def test_reset_password_without_a_configured_email_fails_closed(mem):
    core = _core(mem)   # the bare _core() helper's settings has no .get() -> unconfigured
    reply = core.start_password_reset()
    assert "No password-reset email is configured" in reply
    assert core.awaiting_password_reset_code() is False


def test_reset_password_emails_a_code_to_the_configured_address(mem):
    core, mailer = _core_with_mailer(mem)
    reply = core.start_password_reset()
    assert "Sent a verification code" in reply
    assert len(mailer.sent) == 1
    assert mailer.sent[0]["to"] == "okmomanyi@gmail.com"
    assert core.awaiting_password_reset_code() is True


def test_reset_password_never_sends_the_code_over_telegram(mem):
    """The email IS the second factor -- Telegram only ever gets a confirmation that it was sent."""
    core, mailer = _core_with_mailer(mem)
    reply = core.start_password_reset()
    code = _extract_code(mailer.sent[0]["body"])
    assert code not in reply


def test_wrong_code_does_not_end_the_session(mem):
    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    reply, self_destruct = core.continue_password_reset("000000")
    assert self_destruct is False
    assert "didn't match" in reply
    assert core.awaiting_password_reset_code() is True


def test_cancel_ends_the_session_without_touching_the_password(mem):
    from core.auth import get_store

    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    reply, self_destruct = core.continue_password_reset("cancel")
    assert self_destruct is False
    assert "Cancelled" in reply
    assert core.awaiting_password_reset_code() is False
    assert get_store(mem).has_password() is False


def test_correct_code_resets_the_password_and_flags_self_destruct(mem):
    from core.auth import get_store

    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    code = _extract_code(mailer.sent[0]["body"])

    reply, self_destruct = core.continue_password_reset(code)

    assert self_destruct is True
    assert core.awaiting_password_reset_code() is False
    new_password = _extract_password(reply)
    assert get_store(mem).verify_password(new_password) is True


def test_the_code_never_appears_in_the_success_reply(mem):
    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    code = _extract_code(mailer.sent[0]["body"])
    reply, _ = core.continue_password_reset(code)
    assert code not in reply


def test_a_used_code_cannot_be_replayed(mem):
    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    code = _extract_code(mailer.sent[0]["body"])
    core.continue_password_reset(code)

    # Start a fresh session (as if /resetpassword were run again) and try the OLD code.
    core.start_password_reset()
    reply, self_destruct = core.continue_password_reset(code)
    assert self_destruct is False
    assert "didn't match" in reply


def test_repeated_wrong_codes_eventually_lock_out_and_end_the_session(mem, monkeypatch):
    from core.auth import AuthStore

    now = [1_000_000.0]
    monkeypatch.setattr("core.auth.get_store",
                        lambda memory=None: AuthStore(memory=mem, clock=lambda: now[0]))
    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()

    for _ in range(5):   # password_reset_otp's threshold
        now[0] += 20   # clears each step's backoff so failures actually accumulate
        reply, self_destruct = core.continue_password_reset("000000")
        assert self_destruct is False
        assert "didn't match" in reply

    now[0] += 20
    reply, self_destruct = core.continue_password_reset("000000")
    assert self_destruct is False
    assert "locked" in reply.lower()
    assert core.awaiting_password_reset_code() is False


def test_a_stale_session_stops_intercepting_replies(mem, monkeypatch):
    """Mirrors the existing 45-min-TTL session tests: a forgotten reset code prompt must not
    swallow tomorrow's unrelated message."""
    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 601)   # past the 10-minute TTL
    assert core.awaiting_password_reset_code() is False


def test_password_reset_revokes_other_sessions_but_not_devices(mem):
    from core.auth import get_store

    store = get_store(mem)
    store.create_session(user_agent="chrome")
    device = store.issue_device_credential("Kelvin Laptop")

    core, mailer = _core_with_mailer(mem)
    core.start_password_reset()
    code = _extract_code(mailer.sent[0]["body"])
    reply, _ = core.continue_password_reset(code)

    assert "other signed-in session" in reply
    assert store.validate_session(device) is not None
