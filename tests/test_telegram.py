"""Telegram BotCore tests: authorization, command routing, inline callbacks, job buttons,
mock continuation, status snapshot, voice-note transcription. No network / no PTB runtime."""

from __future__ import annotations

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


def test_jobs_payload_empty(mem):
    core = _core(mem)
    header, jobs = core.jobs_payload()
    assert jobs == []


# ------------------------------------------------------------------ voice notes
def test_transcribe_injection(mem):
    core = BotCore(registry=_FakeRegistry(), memory=mem, transcribe=lambda p: "check my email")
    assert core.transcribe("/tmp/x.ogg") == "check my email"
