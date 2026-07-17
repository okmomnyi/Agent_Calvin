"""Job hunter pipeline tests: dedupe, approval-gate (never send until approved),
email vs portal apply routing, AUTO_APPLY, and the interview watcher — all offline."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from core.llm import LLMClient
from skills.job_hunter.skill import JobHunterSkill
from skills.job_hunter.sources.base import RawJob


class _FakeSource:
    enabled = True

    def __init__(self, name, jobs):
        self.name = name
        self._jobs = jobs

    def fetch(self):
        return self._jobs


class _HuntLLM(LLMClient):
    """Scores by matching job title; writes a fixed cover; classifies interview mail."""

    def __init__(self, scores):
        self.routes = {"default": "m", "classify": "m", "write": "m"}
        self.defaults = {}
        self.scores = scores  # title-substring -> (score, category)

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        blob = " ".join(m["content"] for m in messages)
        for key, (sc, cat) in self.scores.items():
            if key in blob:
                return {"score": sc, "category": cat, "reason": f"{cat} fit", "unpaid": False}
        return {"score": 0, "category": "other", "reason": "no", "unpaid": False}

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "Hi, I'm interested in this role. Best, Calvin"

    def classify(self, text, labels, **kw):  # type: ignore[override]
        return "interview_invite" if "interview" in text.lower() else "unrelated"


def _email_job():
    return RawJob(source="remoteok", external_id="e1", title="DevOps Engineer", company="Acme",
                  url="https://r.ok/e1", description="k8s", apply_email="jobs@acme.com",
                  category_hint="cloud_devops")


def _portal_job():
    return RawJob(source="remotive", external_id="p1", title="Cloud Support", company="Nimbus",
                  url="https://remotive/p1", description="aws", category_hint="cloud_devops")


def _weak_job():
    return RawJob(source="remotive", external_id="w1", title="Marketing Manager", company="X",
                  description="ads", category_hint="other")


def _skill(sources, llm, mem, mailer, prep=None, notify=None):
    # prep is injected (a mock by default) so the interview watcher never fires the real
    # Phase-6 prep pack, which would hit live search + the LLM.
    # notify likewise: without it the watcher test pushed a real "Interview invite detected!
    # From: hr@acme.com" to Calvin's phone on every suite run.
    return JobHunterSkill(llm=llm, memory=mem, sources=sources, mailer=mailer,
                          prep=prep or MagicMock(), notify=notify or MagicMock())


def test_hunt_scores_keeps_and_never_sends(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops"), "Marketing": (20, "other")})
    mailer = MagicMock()
    src = _FakeSource("remoteok", [_email_job(), _weak_job()])
    skill = _skill([src], llm, mem, mailer)

    result = skill.hunt(notify=False)
    assert result.data["new"] == 2
    assert result.data["kept"] == 1          # weak job dropped below threshold
    # APPROVAL GATE: hunting alone must NEVER send an application
    assert mailer.send_application.called is False

    # keeper persisted as drafted with a cover and an email apply route
    row = mem.get_job_by_ref("remoteok", "e1")
    assert row["status"] == "notified"
    assert row["apply_kind"] == "email"
    assert row["cover_text"]
    # weak job marked skipped, not applied
    assert mem.get_job_by_ref("remotive", "w1")["status"] == "skipped"


def test_hunt_is_idempotent(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    src = _FakeSource("remoteok", [_email_job()])
    skill = _skill([src], llm, mem, MagicMock())

    first = skill.hunt(notify=False)
    second = skill.hunt(notify=False)
    assert first.data["new"] == 1
    assert second.data["new"] == 0           # already seen -> nothing reprocessed


def test_approve_email_job_sends_once_with_application_recorded(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    mailer = MagicMock()
    skill = _skill([_FakeSource("remoteok", [_email_job()])], llm, mem, mailer)
    skill.hunt(notify=False)
    job_id = mem.get_job_by_ref("remoteok", "e1")["id"]

    result = skill.approve(selection=[job_id])
    assert mailer.send_application.call_count == 1
    sent = mailer.send_application.call_args.kwargs
    assert sent["to"] == "jobs@acme.com"
    assert job_id in result.data["applied"]
    # application tracked + job marked applied
    assert mem.get_job(job_id)["status"] == "applied"
    assert "Acme" in mem.applied_company_names()


def test_approve_portal_job_tracks_without_sending(fake_settings, mem):
    llm = _HuntLLM({"Cloud Support": (75, "cloud_devops")})
    mailer = MagicMock()
    skill = _skill([_FakeSource("remotive", [_portal_job()])], llm, mem, mailer)
    skill.hunt(notify=False)
    job_id = mem.get_job_by_ref("remotive", "p1")["id"]

    result = skill.approve(selection=[job_id])
    assert mailer.send_application.called is False   # portal => no auto email
    assert job_id in result.data["manual"]
    assert mem.get_job(job_id)["status"] == "applied"


def test_auto_apply_sends_email_jobs_during_hunt(fake_settings_autoapply, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    mailer = MagicMock()
    skill = _skill([_FakeSource("remoteok", [_email_job()])], llm, mem, mailer)

    result = skill.hunt(notify=False)
    assert result.data["auto_applied"] == 1
    assert mailer.send_application.call_count == 1   # AUTO_APPLY relaxed the gate for email-apply


def test_interview_watcher_alerts_on_invite(fake_settings, mem):
    llm = _HuntLLM({})
    prep = MagicMock()
    notify = MagicMock()
    skill = _skill([], llm, mem, MagicMock(), prep=prep, notify=notify)
    # record an application so there's a company to match against
    mem.record_application(job_id=None, company="Acme", source="remoteok", category="cloud_devops")

    msgs = [{"gmail_id": "m1", "sender": "hr@acme.com",
             "subject": "Interview invitation for DevOps role", "snippet": "let's schedule"}]
    result = skill.interview_check(messages=msgs)
    assert result.data["alerts"] == 1
    # the invite auto-fires the Phase-6 prep pack for the matched company
    prep.prep.assert_called_once()
    assert "Acme" in prep.prep.call_args.kwargs["company"]
    # the alert goes out -- to the INJECTED notifier, never the real Telegram
    notify.assert_called_once()
    assert "Interview invite detected" in notify.call_args.args[0]
    # idempotent: same message won't alert twice
    again = skill.interview_check(messages=msgs)
    assert again.data["alerts"] == 0
    assert notify.call_count == 1
