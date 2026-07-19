"""Durable job queue + worker split (Phase 26).

The properties that make `--scale worker=3` safe and make the 40-job cap unnecessary:
  * two workers never claim the same row (FOR UPDATE SKIP LOCKED);
  * a failing job retries with backoff, then fails loudly -- it is never lost or retried forever;
  * nothing is ever deleted, so a failure can be inspected and requeued (§0 P4);
  * a handler that does not exist fails fast rather than looping against an image that will
    never know how to run it.
"""

from __future__ import annotations

import pytest

from core.queue import DONE, FAILED, QUEUED, RUNNING, JobQueue, handler, registered


@pytest.fixture
def q(mem):
    return JobQueue(memory=mem, clock=lambda: 1_000.0)


def test_enqueue_then_claim_roundtrip(q):
    jid = q.enqueue("test.echo", {"msg": "hi"})
    assert jid
    job = q.claim(worker="w1")
    assert job and job.kind == "test.echo" and job.payload == {"msg": "hi"}
    assert job.attempts == 1


def test_a_claimed_job_is_not_handed_to_a_second_worker(q):
    """The guarantee behind `docker compose up --scale worker=3`."""
    q.enqueue("test.echo", {"n": 1})
    first = q.claim(worker="w1")
    second = q.claim(worker="w2")
    assert first is not None
    assert second is None, "two workers claimed the same job -- it would run twice"


def test_workers_take_different_jobs(q):
    q.enqueue("test.echo", {"n": 1})
    q.enqueue("test.echo", {"n": 2})
    a = q.claim(worker="w1")
    b = q.claim(worker="w2")
    assert a and b and a.id != b.id


def test_dedupe_key_stops_the_same_work_queueing_twice(q):
    """Re-running a hunt while the backlog drains must not double-queue a job."""
    assert q.enqueue("job_hunter.score_one", {"job_id": 7}, dedupe_key="score:7") is not None
    assert q.enqueue("job_hunter.score_one", {"job_id": 7}, dedupe_key="score:7") is None
    # once it is finished, the same key may be queued again
    job = q.claim()
    q.complete(job.id)
    assert q.enqueue("job_hunter.score_one", {"job_id": 7}, dedupe_key="score:7") is not None


def test_future_run_at_is_not_claimed_yet(q):
    q.enqueue("test.echo", {}, run_at=2_000.0)      # clock is at 1000
    assert q.claim() is None


# ================================================================= execution
def test_run_one_executes_the_registered_handler(q):
    seen = []

    @handler("test.records")
    def _record(**kw):
        seen.append(kw)
        return "ok"

    q.enqueue("test.records", {"a": 1})
    job = q.run_one()
    assert job is not None and seen == [{"a": 1}]
    assert q.stats()[DONE] == 1


def test_a_failing_job_retries_with_backoff_then_fails(q, mem):
    @handler("test.boom")
    def _boom(**_kw):
        raise RuntimeError("nope")

    q.enqueue("test.boom", {}, max_attempts=2)
    q.run_one()                                     # attempt 1 -> requeued with backoff
    row = mem.execute("SELECT status, run_at, last_error FROM job_queue").fetchone()
    assert row["status"] == QUEUED
    assert row["run_at"] > 1_000.0, "retry was not backed off"
    assert "nope" in row["last_error"]

    q._now = lambda: 9_999.0                        # jump past the backoff
    q.run_one()                                     # attempt 2 -> exhausted
    row = mem.execute("SELECT status, attempts FROM job_queue").fetchone()
    assert row["status"] == FAILED and row["attempts"] == 2


def test_a_failed_job_is_kept_and_can_be_requeued(q, mem):
    @handler("test.boom2")
    def _boom(**_kw):
        raise RuntimeError("bad")

    q.enqueue("test.boom2", {}, max_attempts=1)
    q.run_one()
    assert q.stats()[FAILED] == 1
    assert q.recent_failures()[0]["kind"] == "test.boom2"
    assert q.requeue_failed("test.boom2") == 1      # after a fix
    assert q.stats()[QUEUED] == 1
    # §0 P4: the row was never deleted
    assert mem.execute("SELECT COUNT(*) c FROM job_queue").fetchone()["c"] == 1


def test_an_unknown_kind_fails_immediately_not_forever(q, mem):
    q.enqueue("nobody.registered.this", {})
    q.run_one()
    row = mem.execute("SELECT status, last_error FROM job_queue").fetchone()
    assert row["status"] == FAILED
    assert "no handler" in row["last_error"]


def test_a_worker_survives_a_bad_job(q):
    @handler("test.explodes")
    def _explode(**_kw):
        raise ValueError("boom")

    q.enqueue("test.explodes", {})
    q.enqueue("test.explodes", {})
    q.run_one()
    assert q.run_one() is not None, "the worker stopped after one bad job"


def test_idle_queue_returns_none(q):
    assert q.run_one() is None


# ================================================================= the 40-job cap
def test_hunt_overflow_is_queued_not_discarded(mem, fake_settings, monkeypatch):
    """"(438 more deferred to the next run)" left 432 jobs unscored for days.

    The per-run cap bounds ONE pass; the rest belongs on the queue.
    """
    from unittest.mock import MagicMock

    from skills.job_hunter.skill import JobHunterSkill
    from skills.job_hunter.sources.base import RawJob

    monkeypatch.setattr("skills.job_hunter.skill.get_queue",
                        lambda: JobQueue(memory=mem, clock=lambda: 1_000.0))

    class _Src:
        name, enabled = "fake", True

        def fetch(self):
            return [RawJob(source="fake", external_id=str(i), title=f"DevOps Engineer {i}",
                           company="Acme", description="docker kubernetes")
                    for i in range(5)]

    class _LLM:
        routes, defaults = {"classify": "m"}, {}

        def chat_json(self, *a, **k):
            return {"score": 10, "category": "other", "reason": "low"}

    skill = JobHunterSkill(llm=_LLM(), memory=mem, sources=[_Src()], mailer=MagicMock(),
                           prep=MagicMock(), notify=MagicMock(), cv_tailor=MagicMock())
    monkeypatch.setattr(type(skill), "max_score_per_run", property(lambda self: 2))

    res = skill.hunt(notify=False)
    assert res.data["new"] == 5
    assert res.data["scored"] == 2                  # this pass stayed bounded
    assert res.data["queued"] == 3, "the overflow was dropped instead of queued"
    assert res.data["deferred"] == 0                # nothing silently lost
    assert JobQueue(memory=mem).stats()[QUEUED] == 3


def test_the_score_handler_is_registered_for_workers():
    """A worker resolves handlers by NAME; if this is missing the backlog never drains."""
    import skills.job_hunter.skill  # noqa: F401  (import registers it)

    assert "job_hunter.score_one" in registered()


# ================================================================= scheduled -> queued
def test_heavy_scheduled_jobs_are_routed_to_the_queue():
    """Scraping, transcription and embedding must not run inside the API process.

    A 6-hourly hunt or a 10-minute transcription pass competing with /api/command is what
    made the API feel slow; queued, they get a worker, retries and visibility instead.
    """
    from kernel.registry import SkillRegistry

    registry = SkillRegistry()
    registry.discover()
    queued = {j.id for j in registry.all_scheduled_jobs() if getattr(j, "queued", False)}
    for heavy in ("job_hunter.hunt", "lecture.inbox", "vault.ingest", "flip.scan", "events.scan"):
        assert heavy in queued, f"{heavy} still runs in the API process"


def test_every_queued_job_names_a_real_skill_action():
    """A queued row dispatches by NAME; a typo would fail silently every time it fired."""
    from kernel.registry import SkillRegistry

    registry = SkillRegistry()
    registry.discover()
    for job in registry.all_scheduled_jobs():
        if not getattr(job, "queued", False):
            continue
        assert job.skill and job.action, f"{job.id} is queued but names no skill/action"
        skill = registry.get(job.skill)
        assert skill is not None, f"{job.id} -> unknown skill {job.skill!r}"
        assert job.action in skill.commands(), f"{job.id} -> {job.skill} has no {job.action!r}"


def test_light_jobs_still_run_inline():
    """Not everything belongs on the queue: a 2-second no-op tick would just add latency."""
    from kernel.registry import SkillRegistry

    registry = SkillRegistry()
    registry.discover()
    inline = {j.id for j in registry.all_scheduled_jobs() if not getattr(j, "queued", False)}
    assert "music.session_tick" in inline
    assert "planner.briefing" in inline


def test_skill_run_handler_dispatches(mem, monkeypatch):
    from core.queue import run_skill

    calls = []

    class _FakeSkill:
        name = "faker"

        def commands(self):
            return {"go": lambda **kw: type("R", (), {"text": f"ran {kw}"})()}

    class _Reg:
        def discover(self): pass
        def get(self, name): return _FakeSkill() if name == "faker" else None

    monkeypatch.setattr("kernel.registry.get_registry", lambda: _Reg())
    out = run_skill(skill="faker", action="go", n=1)
    assert "ran" in out


def test_skill_run_fails_loudly_on_a_bad_target(mem, monkeypatch):
    from core.queue import run_skill

    class _Reg:
        def discover(self): pass
        def get(self, name): return None

    monkeypatch.setattr("kernel.registry.get_registry", lambda: _Reg())
    with pytest.raises(RuntimeError, match="unknown skill"):
        run_skill(skill="ghost", action="go")


def test_a_slow_scheduled_run_does_not_stack_up_copies(q):
    """The timer fires every 6h; if the previous run is still draining, don't queue another."""
    assert q.enqueue("skill.run", {"skill": "job_hunter", "action": "hunt"},
                     dedupe_key="sched:job_hunter.hunt") is not None
    assert q.enqueue("skill.run", {"skill": "job_hunter", "action": "hunt"},
                     dedupe_key="sched:job_hunter.hunt") is None


def test_every_deployment_path_runs_a_queue_worker():
    """A scheduler that enqueues into a queue nobody drains fails SILENTLY.

    `ecosystem.config.js` defined only agentos-api + agentos-bot for the whole of Phase 26,
    while the scheduler had already been switched to enqueue the heavy jobs (hunt, ingest,
    lecture inbox, flip/event scans, the 05:30 triage). On the documented PM2 deployment
    those rows were claimed by nobody: the hunt stopped finding jobs and triage never ran,
    with nothing in the logs, because the enqueue itself succeeded.

    Docker was always correct; this asserts the two paths cannot drift apart again.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    pm2 = (root / "ecosystem.config.js").read_text(encoding="utf-8")
    assert "kernel.worker" in pm2, "ecosystem.config.js starts no queue worker"

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "kernel.worker" in compose, "docker-compose.yml starts no queue worker"
