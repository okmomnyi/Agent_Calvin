"""Job hunter pipeline tests: dedupe, approval-gate (never send until approved),
email vs portal apply routing, the approval gate, and the interview watcher — all offline."""

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


# ------------------------------------------------------------------ dedup (regression)
# Telegram log: [9714] and [9710], identical title + company, scored 95 and 85 -- the only
# dedup was UNIQUE(source, external_id), which never catches the same real posting arriving
# under two different ids (two source feeds, or the board listing it twice).
def test_a_duplicate_posting_from_a_different_source_id_is_skipped_not_scored(fake_settings, mem):
    # Deliberately no seniority keyword in the title -- this test is isolating dedup, not
    # interacting with #4's seniority multiplier.
    llm = _HuntLLM({"Software Engineer": (95, "cloud_devops")})
    dup_a = RawJob(source="remoteok", external_id="d1",
                   title="Software Engineer, Infrastructure - Compute Platform",
                   company="Coinbase", description="k8s")
    dup_b = RawJob(source="remotive", external_id="d2",
                   title="Software Engineer, Infrastructure — Compute Platform",  # em dash
                   company="Coinbase", description="k8s")
    skill = _skill([_FakeSource("remoteok", [dup_a]), _FakeSource("remotive", [dup_b])],
                   llm, mem, MagicMock())

    result = skill.hunt(notify=False)
    assert result.data["kept"] == 1, "only one of the two identical postings should be scored"
    statuses = {r["source"]: r["status"] for r in
               mem.execute("SELECT source, status FROM jobs WHERE company='Coinbase'").fetchall()}
    assert sorted(statuses.values()) == ["notified", "skipped"]


def test_different_companies_with_the_same_title_are_not_deduped(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (80, "cloud_devops")})
    a = RawJob(source="remoteok", external_id="a1", title="DevOps Engineer", company="Acme")
    b = RawJob(source="remotive", external_id="b1", title="DevOps Engineer", company="Nimbus")
    skill = _skill([_FakeSource("remoteok", [a]), _FakeSource("remotive", [b])],
                   llm, mem, MagicMock())
    result = skill.hunt(notify=False)
    assert result.data["kept"] == 2, "same title at two different companies is not a duplicate"


# ------------------------------------------------------------------ cadence (regression, #18)
# Telegram log: digests at 01:31, 07:30, 13:31, 19:30 -- roughly 6h apart, including the
# middle of the night. `trigger="interval", hours=6` only knows ELAPSED time since the
# process booted, never wall-clock time, so it drifts to whatever hour the last restart
# happened to land on. `cron` fires at fixed LOCAL hours regardless of restarts.
def test_hunt_is_scheduled_at_fixed_waking_hours_not_a_raw_interval(fake_settings, mem):
    skill = _skill([], _HuntLLM({}), mem, MagicMock())
    hunt_job = next(j for j in skill.scheduled_jobs() if j.id == "job_hunter.hunt")
    assert hunt_job.trigger == "cron", \
        "an interval trigger can land at any hour, including 1:30am -- must be cron"
    hours = {int(h) for h in str(hunt_job.kwargs["hour"]).split(",")}
    assert hours.issubset(set(range(6, 22))), \
        f"scheduled hours must be during the day, got {hours}"


def test_hunt_is_idempotent(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    src = _FakeSource("remoteok", [_email_job()])
    skill = _skill([src], llm, mem, MagicMock())

    first = skill.hunt(notify=False)
    second = skill.hunt(notify=False)
    assert first.data["new"] == 1
    assert second.data["new"] == 0           # already seen -> nothing reprocessed


# ------------------------------------------------------------------ digest ordering (regression)
# Telegram log evidence: with scores [85, 80, 80, 80] the bot suggested the three 80s and
# OMITTED the 85 -- `_render_digest` sorted the DISPLAYED list but the suggestion line read
# from the original, unsorted `keepers` argument (id order), a second independent expression
# that silently diverged from the first.
def _keeper(id_, score, title="Role"):
    return {"id": id_, "title": title, "company": "Acme", "score": score,
           "category": "cloud_devops", "apply_kind": "portal", "apply_target": None,
           "url": "https://x/1", "summary": "s"}


def test_digest_shows_an_honest_placeholder_when_company_is_missing(fake_settings, mem):
    """Regression (#19): himalayas.app-style postings can arrive with no company at all --
    "[3589] DevOps Engineer @ " read as broken. Never guess a name; say so plainly."""
    skill = _skill([], _HuntLLM({}), mem, MagicMock())
    keepers = [_keeper(1, 80, title="DevOps Engineer")]
    keepers[0]["company"] = ""
    text = skill._render_digest(keepers, deferred=0)
    assert "company not listed" in text.lower()
    assert "@   (" not in text  # no bare blank between @ and the score


def test_digest_suggestion_matches_the_displayed_ranking(fake_settings, mem):
    skill = _skill([], _HuntLLM({}), mem, MagicMock())
    # Lower ids score lower; the 85 has the HIGHEST id, so an id-ordered slice omits it --
    # exactly the 20/07 13:31 log entry (scores 85,80,80,80 -> suggested 3320,3422,3584).
    keepers = [_keeper(100, 80), _keeper(101, 80), _keeper(102, 80), _keeper(103, 85)]
    text = skill._render_digest(keepers, deferred=0)
    suggested_line = next(line for line in text.splitlines() if "Reply `approve" in line)
    suggested_ids = {int(x) for x in
                     suggested_line.split("approve ", 1)[1].split("`", 1)[0].split(",")}
    assert 103 in suggested_ids, f"the 85-scored job was omitted from the suggestion: {suggested_line!r}"
    assert suggested_ids == {103, 101, 102} or suggested_ids == {103, 100, 101} \
        or suggested_ids == {103, 100, 102}, \
        "suggestion must be the top-3 BY SCORE, not the first 3 in argument order"


def test_digest_suggestion_order_matches_displayed_order(fake_settings, mem):
    """The exact bug shape: the listing (sorted) and the suggestion (unsorted) used to be two
    independent expressions that could disagree even when both individually looked correct."""
    skill = _skill([], _HuntLLM({}), mem, MagicMock())
    keepers = [_keeper(1, 60), _keeper(2, 95), _keeper(3, 70), _keeper(4, 95), _keeper(5, 85)]
    text = skill._render_digest(keepers, deferred=0)
    displayed_order = [int(line.split("]", 1)[0].lstrip("\n["))
                       for line in text.splitlines() if line.strip().startswith("[")]
    suggested_line = next(line for line in text.splitlines() if "Reply `approve" in line)
    suggested_ids = [int(x) for x in
                     suggested_line.split("approve ", 1)[1].split("`", 1)[0].split(",")]
    assert suggested_ids == displayed_order[:3], \
        "the suggestion must be a prefix of the same ranking shown above it"


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
    # Regression (#2): a tracked portal row must be distinguishable from a real send, both
    # in the applications table (kind/status) and in the weekly report's counters.
    row = mem.execute("SELECT status, kind FROM applications WHERE job_id=%s", (job_id,)).fetchone()
    assert row["kind"] == "portal"
    assert row["status"] == "tracked", "a portal-tracked row must never carry status='applied'"


# ------------------------------------------------------------------ sent vs tracked (regression)
# Telegram log: "Applications sent: 3" in a week where every job was `apply on site` -- nothing
# was actually emailed. record_application()'s kind/status split, and report()'s wording, must
# never let a tracked row read as "sent" again.
def test_report_never_counts_a_tracked_portal_job_as_sent(fake_settings, mem):
    llm = _HuntLLM({"Cloud Support": (75, "cloud_devops")})
    skill = _skill([_FakeSource("remotive", [_portal_job()])], llm, mem, MagicMock())
    skill.hunt(notify=False)
    job_id = mem.get_job_by_ref("remotive", "p1")["id"]
    skill.approve(selection=[job_id])

    result = skill.report(notify=False)
    assert result.data["emailed"] == 0
    assert result.data["tracked"] == 1
    assert "sent by email: 0" in result.text.lower()
    assert "sent: 3" not in result.text.lower()  # the exact untrue phrasing from the log


def test_report_splits_emailed_and_tracked_side_by_side(fake_settings, mem):
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops"), "Cloud Support": (75, "cloud_devops")})
    mailer = MagicMock()
    skill = _skill([_FakeSource("remoteok", [_email_job()]),
                    _FakeSource("remotive", [_portal_job()])], llm, mem, mailer)
    skill.hunt(notify=False)
    email_id = mem.get_job_by_ref("remoteok", "e1")["id"]
    portal_id = mem.get_job_by_ref("remotive", "p1")["id"]
    skill.approve(selection=[email_id, portal_id])

    result = skill.report(notify=False)
    assert result.data["emailed"] == 1
    assert result.data["tracked"] == 1
    # response rate must be computed against the emailed count, not total rows (2) --
    # otherwise a portal job with no possible reply silently drags the rate down.
    assert result.data["by_status"].get("interview", 0) == 0
    assert "rate 0%" in result.text.lower()  # 0 replies / 1 emailed, not / 2 total


def test_hunt_never_sends_an_application_by_itself(fake_settings, mem):
    """§0 P3: a scheduled hunt drafts and notifies. It does not apply.

    `AUTO_APPLY=true` used to make `hunt()` send email-apply keepers outright, with no
    per-job approval and no cap -- a single config flag that reached "never ask before
    sending in Calvin's name". Phase 30 keeps `high` out of LEARNABLE_TIERS so that no
    amount of saying yes can teach the system to stop asking; a flag doing the same thing
    in one line was the same hole spelled differently, so the bypass was removed.

    Asserting on the *unconditional* behaviour of hunt() means reintroducing any such flag
    fails here, whatever it ends up being called.
    """
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    mailer = MagicMock()
    skill = _skill([_FakeSource("remoteok", [_email_job()])], llm, mem, mailer)

    result = skill.hunt(notify=False)

    assert mailer.send_application.called is False, "hunt() sent an application unprompted"
    assert result.data["kept"] == 1                  # it still found and drafted the job
    job_id = mem.get_job_by_ref("remoteok", "e1")["id"]
    assert mem.get_job(job_id)["status"] == "notified"   # awaiting him, not applied

    # ...and the application still goes out the moment he approves it.
    skill.approve(selection=[job_id])
    assert mailer.send_application.call_count == 1


def test_no_settings_flag_can_relax_the_application_gate():
    """The structural half: no `auto_apply`-shaped escape hatch exists on Settings at all."""
    from core.config import Settings

    fields = set(Settings.__dataclass_fields__)
    offenders = {f for f in fields if "auto_apply" in f or "auto_send" in f}
    assert not offenders, f"a send-without-approval flag is back on Settings: {offenders}"


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


def test_approve_auto_tailors_cv_and_confirms(fake_settings, mem):
    """Calvin's ask: on approval, tailor the CV to the role, then apply, then confirm.

    The tailorer is injected, so no LLM is touched. The master CV is never involved here --
    cv_tailor writes variants only -- so this asserts the wiring: tailor is called with the
    job id, and the confirmation is in the returned text.

    Regression (#15): approve() must NOT ALSO push via the injected `notify` -- the caller's
    own channel (Telegram's reply, dashboard, TTS) already delivers CommandResult.text.
    Calling notify() too was a real double-send: "Tracked 1 portal/notify job(s)..." arrived
    once bare (the reply) and once wrapped in "📨 Application update:" moments apart.
    """
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    mailer = MagicMock()
    notify = MagicMock()
    tailor = MagicMock()
    tailor.tailor.return_value = type("R", (), {
        "ok": True, "data": {"variant": "data/cv/variants/acme.md",
                             "ats_before": 40, "ats_after": 72}})()
    skill = _skill([_FakeSource("remoteok", [_email_job()])], llm, mem, mailer,
                   notify=notify)
    skill._cv_tailor = tailor
    skill.hunt(notify=False)
    job_id = mem.get_job_by_ref("remoteok", "e1")["id"]

    result = skill.approve(selection=[job_id])
    # tailored to THIS job before applying
    tailor.tailor.assert_called_once()
    assert tailor.tailor.call_args.kwargs["job_id"] == job_id
    # applied, and the confirmation is in the ONE reply -- not pushed a second time
    assert job_id in result.data["applied"]
    assert "72" in result.text        # the ATS lift is reported
    notify.assert_not_called()


def test_approve_still_applies_when_tailoring_fails(fake_settings, mem):
    """A tailoring failure (LLM down, like the NIM timeouts) must not block the application."""
    llm = _HuntLLM({"DevOps Engineer": (85, "cloud_devops")})
    mailer = MagicMock()
    tailor = MagicMock()
    tailor.tailor.side_effect = RuntimeError("NIM timed out")
    skill = _skill([_FakeSource("remoteok", [_email_job()])], llm, mem, mailer,
                   notify=MagicMock())
    skill._cv_tailor = tailor
    skill.hunt(notify=False)
    job_id = mem.get_job_by_ref("remoteok", "e1")["id"]

    result = skill.approve(selection=[job_id])
    assert job_id in result.data["applied"]        # applied anyway
    assert mailer.send_application.call_count == 1
