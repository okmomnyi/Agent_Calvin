"""Job expiry (Phase 34).

Calvin asked for this twice, and the queue reached 83 drafted jobs before it existed. The
tests that matter here are the ones about what expiry must NOT do: it must not delete, and it
must not touch work he has already acted on.
"""

from __future__ import annotations

import json
import time

import pytest

from core.expiry import DAY, JobExpiry, parse_deadline

# --------------------------------------------------------------------- deadline parsing
NOW = 1_800_000_000.0  # fixed clock; parse_deadline sanity-checks against "now"


@pytest.mark.parametrize("text", [
    "Application deadline: 25 December 2026",
    "Apply by 25th Dec 2026 to be considered",
    "Closing date December 25, 2026",
    "applications close 2026-12-25",
    "Last date 25/12/2026",
    "Submit by Dec 25 2026",
])
def test_parses_real_deadline_phrasing(text):
    stamp = parse_deadline(text, now=NOW)
    assert stamp is not None, f"missed: {text}"
    assert time.strftime("%Y-%m-%d", time.localtime(stamp)) == "2026-12-25"


@pytest.mark.parametrize("text", [
    "",
    "Founded in 2019, we are a fast-growing team",
    "Start date: 25 December 2026",          # a date, but not a deadline
    "5+ years experience since 2019",
    "Deadline: the 31st of February 2026",   # not a real day
    "Deadline: 25 December 2199",            # parse artefact, decades out
])
def test_refuses_to_guess_a_deadline(text):
    """None is the safe answer. A job wrongly given a PAST deadline vanishes immediately,
    which is far worse than falling back to the staleness rule."""
    assert parse_deadline(text, now=NOW) is None, f"invented a deadline from: {text!r}"


# --------------------------------------------------------------------- expiry rules
def _job(mem, *, ext: str, status: str = "drafted", age_days: float = 0.0,
         deadline: float | None = None, now: float | None = None,
         score: int | None = None) -> int:
    now = now if now is not None else time.time()
    mem.upsert_job("test", ext, title=f"Role {ext}", company="Acme",
                   raw_json=json.dumps({"description": ""}))
    row = mem.get_job_by_ref("test", ext)
    with mem.tx():
        mem.conn.execute(
            "UPDATE jobs SET status=%s, first_seen=%s, deadline=%s, score=%s WHERE id=%s",
            (status, now - age_days * DAY, deadline, score, row["id"]))
    return row["id"]


def test_retires_jobs_unreviewed_for_two_days(mem):
    """Calvin: "when a job stays unreviewed for two days since it was listed"."""
    now = time.time()
    old = _job(mem, ext="old", age_days=3, now=now)
    fresh = _job(mem, ext="fresh", age_days=0.5, now=now)

    result = JobExpiry(mem).run(now=now)

    assert [r["id"] for r in result.stale] == [old]
    assert mem.get_job(old)["status"] == "expired"
    assert mem.get_job(fresh)["status"] == "drafted", "retired a job he hasn't had time to see"


def test_retires_jobs_past_their_deadline_however_young(mem):
    now = time.time()
    closed = _job(mem, ext="closed", age_days=0.1, deadline=now - DAY, now=now)
    open_ = _job(mem, ext="open", age_days=0.1, deadline=now + 5 * DAY, now=now)

    result = JobExpiry(mem).run(now=now)

    assert [r["id"] for r in result.expired] == [closed]
    assert mem.get_job(open_)["status"] == "drafted"


def test_a_job_both_stale_and_closed_is_reported_as_closed(mem):
    """The more specific reason is the one that tells him the window actually shut."""
    now = time.time()
    both = _job(mem, ext="both", age_days=5, deadline=now - DAY, now=now)
    result = JobExpiry(mem).run(now=now)
    assert [r["id"] for r in result.expired] == [both]
    assert not result.stale


def test_never_deletes_a_row(mem):
    """§0 Principle 4. Expiry must stay falsifiable -- a DELETE makes a bad heuristic
    impossible to review after the fact."""
    now = time.time()
    job_id = _job(mem, ext="old", age_days=9, now=now)
    JobExpiry(mem).run(now=now)

    row = mem.get_job(job_id)
    assert row is not None, "expiry deleted the row"
    assert row["status"] == "expired"
    assert row["title"] == "Role old", "expiry destroyed the scrape"


@pytest.mark.parametrize("status", ["approved", "applied", "skipped"])
def test_never_touches_work_calvin_already_acted_on(mem, status):
    """A tailored CV, a sent application, an interview that might still land. Age is
    meaningless once he has made the decision."""
    now = time.time()
    job_id = _job(mem, ext=f"acted-{status}", status=status, age_days=30,
                  deadline=now - 10 * DAY, now=now)
    JobExpiry(mem).run(now=now)
    assert mem.get_job(job_id)["status"] == status


def test_is_idempotent(mem):
    now = time.time()
    _job(mem, ext="old", age_days=5, now=now)
    first = JobExpiry(mem).run(now=now)
    second = JobExpiry(mem).run(now=now)
    assert first.total == 1
    assert second.total == 0, "re-running expiry re-reported the same job"


# --------------------------------------------------------------------- reminders
def test_reports_windows_about_to_shut(mem):
    """The other half of the ask: hear about it while applying is still possible."""
    now = time.time()
    soon = _job(mem, ext="soon", deadline=now + 2 * DAY, now=now)
    _job(mem, ext="later", deadline=now + 20 * DAY, now=now)
    _job(mem, ext="gone", deadline=now - DAY, now=now)

    upcoming = JobExpiry(mem).upcoming_deadlines(within_days=3, now=now)

    assert [j["id"] for j in upcoming] == [soon]


def test_upcoming_deadlines_are_soonest_first(mem):
    now = time.time()
    later = _job(mem, ext="b", deadline=now + 2.5 * DAY, now=now)
    sooner = _job(mem, ext="a", deadline=now + 0.5 * DAY, now=now)
    upcoming = JobExpiry(mem).upcoming_deadlines(within_days=3, now=now)
    assert [j["id"] for j in upcoming] == [sooner, later]


def test_backfill_reads_deadlines_out_of_already_scraped_jobs(mem):
    """901 jobs were scraped before the column existed."""
    mem.upsert_job("test", "back", title="Cloud Intern", company="Acme",
                   raw_json=json.dumps({"description": "Apply by 25 December 2026."}))
    assert JobExpiry(mem).backfill_deadlines() == 1
    row = mem.get_job_by_ref("test", "back")
    assert row["deadline"] is not None


def test_summary_names_both_reasons(mem):
    now = time.time()
    _job(mem, ext="stale", age_days=5, now=now)
    _job(mem, ext="closed", deadline=now - DAY, now=now)
    summary = JobExpiry(mem).run(now=now).summary()
    assert "2" in summary and "deadline" in summary and "unreviewed" in summary


# --------------------------------------------------------------------- queue cap (#17)
# Report: "pending approvals climbing 13 -> 34 -> 50 -> 69 over four days" -- low scorers
# that hadn't gone stale YET just kept accumulating, because the only retirement rules were
# age-based. This is the volume-based rule that complements them.
def test_caps_the_active_queue_to_top_n_by_score(mem):
    now = time.time()
    keep_a = _job(mem, ext="a", status="drafted", score=90, now=now)
    keep_b = _job(mem, ext="b", status="notified", score=80, now=now)
    bump_c = _job(mem, ext="c", status="drafted", score=70, now=now)
    bump_d = _job(mem, ext="d", status="notified", score=60, now=now)

    result = JobExpiry(mem, queue_cap=2).run(now=now)

    assert {r["id"] for r in result.capped} == {bump_c, bump_d}
    assert mem.get_job(keep_a)["status"] == "drafted"
    assert mem.get_job(keep_b)["status"] == "notified"
    assert mem.get_job(bump_c)["status"] == "expired"
    assert mem.get_job(bump_d)["status"] == "expired"


def test_cap_only_counts_statuses_actually_shown_to_him(mem):
    """"new"/"scored" haven't been drafted yet -- they can't be part of a queue he is
    failing to review, so they must not count against the cap or get capped themselves."""
    now = time.time()
    shown = _job(mem, ext="shown", status="drafted", score=50, now=now)
    unscored = _job(mem, ext="new", status="new", score=None, now=now)
    scored_only = _job(mem, ext="scored", status="scored", score=99, now=now)

    result = JobExpiry(mem, queue_cap=1).run(now=now)

    assert not result.capped
    assert mem.get_job(shown)["status"] == "drafted"
    assert mem.get_job(unscored)["status"] == "new"
    assert mem.get_job(scored_only)["status"] == "scored"


def test_cap_never_touches_work_already_acted_on(mem):
    now = time.time()
    approved = _job(mem, ext="approved", status="approved", score=1, now=now)
    kept = _job(mem, ext="kept", status="drafted", score=99, now=now)

    JobExpiry(mem, queue_cap=1).run(now=now)

    assert mem.get_job(approved)["status"] == "approved"
    assert mem.get_job(kept)["status"] == "drafted"


def test_cap_prefers_age_and_deadline_reasons_when_they_overlap(mem):
    """A job that is both stale AND over the cap is reported once, under the more specific
    age-based reason -- enforce_cap excludes rows this run already retired."""
    now = time.time()
    stale_and_low = _job(mem, ext="stale-low", status="drafted", score=10, age_days=5, now=now)
    kept = _job(mem, ext="kept", status="drafted", score=90, now=now)

    result = JobExpiry(mem, queue_cap=1).run(now=now)

    assert [r["id"] for r in result.stale] == [stale_and_low]
    assert not result.capped
    assert mem.get_job(kept)["status"] == "drafted"


def test_summary_mentions_the_capped_count(mem):
    now = time.time()
    _job(mem, ext="keep", status="drafted", score=90, now=now)
    _job(mem, ext="cut", status="drafted", score=10, now=now)
    summary = JobExpiry(mem, queue_cap=1).run(now=now).summary()
    assert "1" in summary and "higher-scoring" in summary


def test_cap_is_idempotent(mem):
    now = time.time()
    _job(mem, ext="keep", status="drafted", score=90, now=now)
    _job(mem, ext="cut", status="drafted", score=10, now=now)
    expiry = JobExpiry(mem, queue_cap=1)
    first = expiry.run(now=now)
    second = expiry.run(now=now)
    assert len(first.capped) == 1
    assert not second.capped, "re-running the cap re-retired an already-expired job"
