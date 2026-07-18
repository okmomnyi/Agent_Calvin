"""Durable job queue (Phase 26).

Turns AgentOS from "a scheduler that runs functions in the API process" into an api/worker
split that can actually scale: the API (and the scheduler) ENQUEUE work, and one or more
worker processes CLAIM and run it.

Why this exists, concretely:

* **The 40-job cap.** `jobs.max_score_per_run: 40` and "(438 more deferred to the next run)"
  are not a policy — they are the absence of a queue. Scoring ran inline in the scheduler, so
  a big batch had to be truncated to keep the process responsive. Queued, the overflow is just
  more rows and workers drain them.
* **API contention.** A long scrape or a 60s CV tailor competed with `/api/command`, which is
  what Calvin talks to. Different process now.
* **No retries, no visibility.** A failed scheduled job vanished into the log. Jobs here have
  attempts, a last error, and a status you can query.

Postgres rather than Redis/Celery: the database is already there, already backed up, already
the thing both processes share. `FOR UPDATE SKIP LOCKED` is exactly the primitive a work queue
needs, so two workers never claim the same row. One less service to run and reason about --
which is the whole point of splitting on real boundaries rather than adding infrastructure.

Handlers are registered by name (`@handler("job_hunter.score")`), so a queued row references a
string, not a pickled callable: a worker deployed from a newer image can still drain rows
enqueued by an older one.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.queue")

# status: queued -> running -> done | failed  (failed rows are kept, never deleted: §0 P4)
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

DEFAULT_MAX_ATTEMPTS = 3
# Exponential, so a flapping network (Calvin's link drops every few minutes) backs off instead
# of hammering: 30s, 120s, 480s.
RETRY_BASE_SECONDS = 30


_HANDLERS: dict[str, Callable[..., Any]] = {}


def handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a queue handler under a stable name."""
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        _HANDLERS[name] = fn
        return fn
    return wrap


def registered() -> dict[str, Callable[..., Any]]:
    return dict(_HANDLERS)


@dataclass
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    status: str


class JobQueue:
    """Enqueue/claim/complete over the `job_queue` table."""

    def __init__(self, memory: Memory | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    # ------------------------------------------------------------- producing
    def enqueue(self, kind: str, payload: dict[str, Any] | None = None, *,
                dedupe_key: str | None = None, run_at: float | None = None,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> int | None:
        """Add a job. With `dedupe_key`, an identical pending job is not queued twice.

        Returns the row id, or None when deduped -- re-running a scheduled scan must not pile
        up duplicate work if the previous run is still draining.
        """
        now = self._now()
        with self.mem.tx() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT id FROM job_queue WHERE dedupe_key=%s AND status IN (%s,%s)",
                    (dedupe_key, QUEUED, RUNNING)).fetchone()
                if existing:
                    return None
            row = conn.execute(
                "INSERT INTO job_queue(kind, payload, status, attempts, max_attempts, "
                "dedupe_key, run_at, created_at) "
                "VALUES(%s,%s,%s,0,%s,%s,%s,%s) RETURNING id",
                (kind, json.dumps(payload or {}), QUEUED, max_attempts, dedupe_key,
                 run_at or now, now)).fetchone()
        return int(row["id"])

    # ------------------------------------------------------------- consuming
    def claim(self, worker: str = "worker") -> Job | None:
        """Atomically take the next due job.

        SKIP LOCKED is what makes `--scale worker=3` safe: each worker locks a different row
        instead of blocking on the same one, so no job is ever run twice and none is starved.
        """
        now = self._now()
        with self.mem.tx() as conn:
            row = conn.execute(
                "UPDATE job_queue SET status=%s, attempts=attempts+1, started_at=%s, worker=%s "
                "WHERE id = (SELECT id FROM job_queue WHERE status=%s AND run_at<=%s "
                "            ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING id, kind, payload, attempts, status",
                (RUNNING, now, worker, QUEUED, now)).fetchone()
        if not row:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        return Job(id=int(row["id"]), kind=row["kind"], payload=payload or {},
                   attempts=int(row["attempts"]), status=row["status"])

    def complete(self, job_id: int, result: str = "") -> None:
        with self.mem.tx() as conn:
            conn.execute("UPDATE job_queue SET status=%s, finished_at=%s, last_error=NULL, "
                         "result=%s WHERE id=%s",
                         (DONE, self._now(), (result or "")[:2000], job_id))

    def fail(self, job_id: int, error: str, *, attempts: int, max_attempts: int) -> str:
        """Reschedule with backoff, or mark failed once attempts are exhausted."""
        if attempts >= max_attempts:
            with self.mem.tx() as conn:
                conn.execute("UPDATE job_queue SET status=%s, finished_at=%s, last_error=%s "
                             "WHERE id=%s", (FAILED, self._now(), error[:2000], job_id))
            return FAILED
        delay = RETRY_BASE_SECONDS * (4 ** (attempts - 1))
        with self.mem.tx() as conn:
            conn.execute("UPDATE job_queue SET status=%s, run_at=%s, last_error=%s WHERE id=%s",
                         (QUEUED, self._now() + delay, error[:2000], job_id))
        return QUEUED

    # ------------------------------------------------------------- running
    def run_one(self, worker: str = "worker") -> Job | None:
        """Claim and execute a single job. Returns the job it ran, or None if idle."""
        job = self.claim(worker=worker)
        if job is None:
            return None
        fn = _HANDLERS.get(job.kind)
        if fn is None:
            # Unknown kind: fail it outright rather than retrying forever against an image
            # that will never know how to run it.
            self.fail(job.id, f"no handler registered for {job.kind!r}",
                      attempts=99, max_attempts=1)
            log.error("queue: no handler for %s (job %s)", job.kind, job.id)
            return job
        try:
            result = fn(**job.payload) if job.payload else fn()
            self.complete(job.id, str(result)[:2000] if result is not None else "")
            log.info("queue: %s #%s done", job.kind, job.id)
        except Exception as exc:  # noqa: BLE001 - a bad job must never kill the worker
            row = self.mem.execute("SELECT max_attempts FROM job_queue WHERE id=%s",
                                   (job.id,)).fetchone()
            outcome = self.fail(job.id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:900]}",
                                attempts=job.attempts,
                                max_attempts=int(row["max_attempts"]) if row else DEFAULT_MAX_ATTEMPTS)
            log.exception("queue: %s #%s %s", job.kind, job.id, outcome)
        return job

    # ------------------------------------------------------------- observability
    def stats(self) -> dict[str, int]:
        rows = self.mem.execute(
            "SELECT status, COUNT(*) AS c FROM job_queue GROUP BY status").fetchall()
        out = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        for r in rows:
            out[r["status"]] = int(r["c"])
        return out

    def recent_failures(self, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(r) for r in self.mem.execute(
            "SELECT id, kind, attempts, last_error, finished_at FROM job_queue "
            "WHERE status=%s ORDER BY finished_at DESC NULLS LAST LIMIT %s",
            (FAILED, limit)).fetchall()]

    def requeue_failed(self, kind: str | None = None) -> int:
        """Put failed jobs back on the queue (after a fix). Never deletes anything."""
        sql = ("UPDATE job_queue SET status=%s, attempts=0, run_at=%s, last_error=NULL "
               "WHERE status=%s")
        params: list[Any] = [QUEUED, self._now(), FAILED]
        if kind:
            sql += " AND kind=%s"
            params.append(kind)
        with self.mem.tx() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount or 0


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
