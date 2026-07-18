"""Job hunter skill orchestration (Phase 3).

Pipeline: scrape (modular sources) -> dedupe (DB) -> category-aware score -> for keepers,
draft a 2-line summary + a cover email grounded only in verified persona facts -> digest
to Telegram with per-job approve/skip guidance. Approval (or AUTO_APPLY for email-apply
sources) triggers the application: email-apply jobs are sent with the CV attached; portal
and notify-only jobs hand Calvin the link + cover. Also tracks applications, produces the
weekly report, and runs the 15-minute interview watcher.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from core.config import get_settings
from core.llm import LLMClient, get_client
from core.logging_setup import get_logger
from core.mailer import ApplicationMailer
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.queue import get_queue, handler
from core.persona_store import get_engine, is_seeded, verified_facts_text
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.scoring import score_job
from skills.job_hunter.sources import build_sources
from skills.job_hunter.sources.base import RawJob

log = get_logger("skills.job_hunter")


class JobHunterSkill(BaseSkill):
    name = "job_hunter"

    def __init__(
        self,
        llm: LLMClient | None = None,
        memory: Memory | None = None,
        fetcher: Fetcher | None = None,
        sources: Sequence[Any] | None = None,
        mailer: ApplicationMailer | None = None,
        prep: Any | None = None,
        notify: Callable[[str], bool] | None = None,
        cv_tailor: Any | None = None,
    ) -> None:
        self._llm = llm
        self._mem = memory
        self._fetcher = fetcher
        # Injectable like every other dependency (infra_recon, music, adaptive all do this).
        # It wasn't, and `interview_check` notifies unconditionally, so the watcher test sent
        # a real "Interview invite detected! From: hr@acme.com" to Calvin's phone on every
        # suite run. The dependency a test cannot replace is the one that reaches a human.
        self._notify = notify or send_telegram
        self._sources = list(sources) if sources is not None else None
        self._mailer = mailer
        self._prep = prep
        self._cv_tailor = cv_tailor

    # lazy deps
    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def sources(self) -> list[Any]:
        if self._sources is None:
            self._sources = build_sources(self._fetcher or Fetcher())
        return self._sources

    @property
    def mailer(self) -> ApplicationMailer:
        if self._mailer is None:
            self._mailer = ApplicationMailer()
        return self._mailer

    @property
    def prep(self):
        """Interview-prep skill (Phase 6), injected in tests so no live research happens."""
        if self._prep is None:
            from skills.interview_prep import SKILL as prep_skill

            self._prep = prep_skill
        return self._prep

    @property
    def cv_tailor(self):
        """CV tailoring skill (Phase 15), injected in tests so no live LLM call happens."""
        if self._cv_tailor is None:
            from skills.cv_tailor import SKILL as cv_skill

            self._cv_tailor = cv_skill
        return self._cv_tailor

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=["jobs", "cv", "tone", "notifications"])

    # ------------------------------------------------------------- skill wiring
    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "hunt": self.hunt,
            "status": self.status,
            "approve": self.approve,
            "report": self.report,
            "watch": self.watch,
            "interview_check": self.interview_check,
            "profile": self.profile,
        }

    def profile(self, tier: str = "", roles: str = "", **_: Any) -> CommandResult:
        """View or edit the job target profile at runtime (stored in memory, survives deploys).

        `profile` alone shows it. `profile primary <roles>` rewrites a tier -- so Calvin can
        steer what "related jobs" means without a redeploy. This is the "not hardcoded, more
        like memory" ask: the config lists are only the seed; whatever he sets here wins.
        """
        import json

        from skills.job_hunter.scoring import _profile_lists

        current = self.mem.kv_get("jobs.profile_override")
        data = json.loads(current) if current else {}
        p, s, a = _profile_lists()
        if not tier:
            return CommandResult(
                text=("🎯 Job target profile"
                      + (" (customised)" if current else " (from config)") + ":\n"
                      f"PRIMARY: {'; '.join(p)}\n"
                      f"SECONDARY: {'; '.join(s)}\n"
                      f"ALSO: {'; '.join(a)}\n\n"
                      "Edit: `profile primary cloud engineer, DevOps, SRE`"),
                data={"primary": p, "secondary": s, "also": a})
        tier = tier.lower().strip()
        if tier not in ("primary", "secondary", "also"):
            return CommandResult(text="Tier must be primary, secondary or also.", ok=False)
        if not roles.strip():
            return CommandResult(text=f"Give me the roles for {tier}, comma-separated.", ok=False)
        data[tier] = [r.strip() for r in roles.split(",") if r.strip()]
        self.mem.kv_set("jobs.profile_override", json.dumps(data))
        return CommandResult(text=f"✅ Updated {tier}. Next hunt scores against your new profile.",
                             data={tier: data[tier]})

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="job_hunter.hunt", func=self.hunt, trigger="interval",
                         kwargs={"hours": 6}),
            ScheduledJob(id="job_hunter.report", func=self.report, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 18, "minute": 0}),
            ScheduledJob(id="job_hunter.interview_check", func=self.interview_check,
                         trigger="interval", kwargs={"minutes": 15}),
        ]

    # ------------------------------------------------------------- config
    @property
    def threshold(self) -> int:
        return int(get_settings().get("jobs", "score_threshold", default=60))

    @property
    def max_score_per_run(self) -> int:
        return int(get_settings().get("jobs", "max_score_per_run", default=40))

    # ------------------------------------------------------------- hunt
    def hunt(self, notify: bool = True, **_: Any) -> CommandResult:
        """Full pipeline over all sources. Idempotent: only newly-seen jobs are processed."""
        new_jobs = self._scrape_new()
        if not new_jobs:
            return CommandResult(text="No new postings since last run.", data={"new": 0})

        # Cap scoring per run to control LLM volume; report anything deferred (no silent cap).
        to_process = new_jobs[: self.max_score_per_run]
        overflow = new_jobs[self.max_score_per_run:]
        # The overflow used to be dropped with "(438 more deferred to the next run)" and, since
        # the next run scraped fresh jobs too, much of it was never scored at all -- 432 jobs
        # sat unscored for days. The cap exists to keep ONE pass bounded, not to discard work,
        # so the rest goes on the queue and workers drain it. Deduped by job id: re-running a
        # hunt while the backlog drains must not queue the same job twice.
        queued = 0
        for job_id, _raw in overflow:
            try:
                if get_queue().enqueue("job_hunter.score_one", {"job_id": int(job_id)},
                                       dedupe_key=f"score:{job_id}") is not None:
                    queued += 1
            except Exception:  # noqa: BLE001 - a queue outage must not abort the hunt
                log.exception("could not enqueue job %s for scoring", job_id)
        deferred = len(overflow) - queued

        keepers: list[dict[str, Any]] = []
        for job_id, raw in to_process:
            keeper = self._score_and_draft(job_id, raw)
            if keeper:
                keepers.append(keeper)

        auto_applied = self._maybe_auto_apply(keepers)
        digest = self._render_digest(keepers, deferred, auto_applied, queued=queued)
        if notify and keepers:
            self._notify(digest)
        for k in keepers:
            self.mem.set_job_status(k["id"], "notified")

        return CommandResult(
            text=digest,
            data={"new": len(new_jobs), "scored": len(to_process), "kept": len(keepers),
                  "queued": queued, "deferred": deferred, "auto_applied": auto_applied},
        )

    def _scrape_new(self) -> list[tuple[int, RawJob]]:
        """Fetch every source, upsert, and return (job_id, RawJob) for NEWLY-seen jobs only."""
        new: list[tuple[int, RawJob]] = []
        for source in self.sources:
            try:
                raws = source.fetch()
            except Exception:  # noqa: BLE001 - one bad source never aborts the hunt
                log.exception("source '%s' fetch failed", getattr(source, "name", "?"))
                continue
            log.info("source '%s' returned %d posting(s)", source.name, len(raws))
            for raw in raws:
                is_new = self.mem.upsert_job(
                    raw.source, raw.external_id, url=raw.url, title=raw.title,
                    company=raw.company, raw_json=json.dumps(raw.to_dict()),
                )
                if is_new:
                    row = self.mem.get_job_by_ref(raw.source, raw.external_id)
                    if row:
                        new.append((row["id"], raw))
        return new

    def _score_and_draft(self, job_id: int, raw: RawJob) -> dict[str, Any] | None:
        """Score a job; for keepers, draft summary+cover and persist. Returns keeper dict or None."""
        if raw.kind == "notify_only":
            category, score, reason = raw.category_hint or "transcription", 100, "Portal signup — apply directly."
        else:
            result = score_job(self.llm, raw)
            category, score, reason = result.category, result.score, result.reason
            self.mem.score_job(job_id, score, category=category, summary=reason)
            if score < self.threshold:
                self.mem.set_job_status(job_id, "skipped")
                log.info("skip '%s' (score %d < %d)", raw.title, score, self.threshold)
                return None

        apply_kind, apply_target = self._apply_route(raw)
        cover = self._draft_cover(raw, category)
        self.mem.save_cover(job_id, apply_kind=apply_kind, apply_target=apply_target, cover_text=cover)
        return {
            "id": job_id, "title": raw.title, "company": raw.company, "score": score,
            "category": category, "summary": reason, "url": raw.url,
            "apply_kind": apply_kind, "apply_target": apply_target, "cover": cover,
        }

    @staticmethod
    def _apply_route(raw: RawJob) -> tuple[str, str | None]:
        if raw.kind == "notify_only":
            return "notify_only", raw.url
        if raw.apply_email:
            return "email", raw.apply_email
        return "portal", raw.url

    def _draft_cover(self, raw: RawJob, category: str) -> str:
        """Draft a cover email using ONLY verified facts (§0: never invent experience)."""
        facts = verified_facts_text(self.mem)
        name = get_settings().my_name
        if not facts:
            return (
                f"[Persona not seeded — run `manage.py persona-init` (Phase 4) so covers can be "
                f"grounded in your verified CV facts. Placeholder below.]\n\n"
                f"Hi,\n\nI'm {name}, a full-stack developer interested in the {raw.title} role"
                f"{' at ' + raw.company if raw.company else ''}. I'd welcome the chance to discuss "
                f"how I can help.\n\nBest,\n{name}"
            )
        sys = (
            f"You draft short, direct, human cover emails as {name}. Rules: use ONLY the verified "
            "facts provided; NEVER invent skills, tools, employers, or experience not listed; if the "
            "job wants something not in the facts, simply don't claim it. 120 words max, no fluff."
        )
        # Honor Calvin's standing instructions (e.g. "prioritize cloud/DevOps") — Phase 4.
        try:
            rules = get_engine().relevant_instructions(["cv", "cover", "job", "cloud", "devops", "apply"])
            if rules:
                sys += " Follow these standing instructions from Calvin: " + "; ".join(rules) + "."
        except Exception:  # noqa: BLE001 - instructions are best-effort context
            pass
        user = (
            f"VERIFIED FACTS ABOUT {name.upper()}:\n{facts}\n\n"
            f"JOB: {raw.title} at {raw.company or 'the company'} ({category}).\n"
            f"Description: {raw.description[:800]}\n\nWrite only the email body."
        )
        try:
            return self.llm.chat("write", [{"role": "system", "content": sys},
                                            {"role": "user", "content": user}], max_tokens=350)
        except Exception:  # noqa: BLE001
            log.exception("cover draft failed for '%s'", raw.title)
            return f"Hi,\n\nI'm interested in the {raw.title} role. Best,\n{name}"

    # ------------------------------------------------------------- auto-apply
    def _maybe_auto_apply(self, keepers: list[dict[str, Any]]) -> int:
        """If AUTO_APPLY is on, auto-send email-apply keepers only. Returns count applied."""
        if not get_settings().auto_apply:
            return 0
        count = 0
        for k in keepers:
            if k["apply_kind"] == "email" and k["apply_target"]:
                if self._send_application(k):
                    count += 1
        return count

    # ------------------------------------------------------------- approve
    def approve(self, selection: Sequence[int] | None = None, **_: Any) -> CommandResult:
        """Approve drafted jobs by id: email-apply => send with CV; portal/notify => record + link."""
        ids = list(selection or [])
        if not ids:
            return CommandResult(text="Nothing to approve — give me job numbers (e.g. approve 1,3).",
                                 ok=False)
        applied, manual, missing, tailored_notes = [], [], [], []
        for job_id in ids:
            job = self.mem.get_job(int(job_id))
            if job is None:
                missing.append(job_id)
                continue
            # Auto-tailor the CV to THIS role before applying (Calvin's ask): a variant that
            # mirrors the job's terminology and lifts the ATS score, built only from verified
            # facts, saved to data/cv/variants/. The master CV is never touched -- cv_tailor
            # only ever writes variants. If tailoring can't run (LLM down) it falls back to the
            # master, so an application is never blocked on it.
            tailored_note = self._auto_tailor(job)
            job = self.mem.get_job(int(job_id)) or job     # re-read: variant path now linked

            keeper = {
                "id": job["id"], "company": job["company"], "source": job["source"],
                "category": job["category"], "apply_kind": job["apply_kind"],
                "apply_target": job["apply_target"], "title": job["title"],
                "cover": job["cover_text"] or "", "cv_variant": job["cv_variant"],
                "tailored_note": tailored_note,
            }
            if job["apply_kind"] == "email" and job["apply_target"]:
                if self._send_application(keeper):
                    applied.append(job_id)
                    tailored_notes.append(f"[{job_id}] {tailored_note}")
            else:  # portal / notify_only — Calvin applies via the link; we just track it
                self.mem.record_application(
                    job_id=job["id"], company=job["company"], source=job["source"],
                    category=job["category"], cv_variant=job["cv_variant"],
                    notes=f"{job['apply_kind']} apply: {job['apply_target']}",
                )
                manual.append(job_id)
                tailored_notes.append(f"[{job_id}] {tailored_note} (apply via link)")

        parts = []
        if applied:
            parts.append(f"✅ Sent {len(applied)} application(s) by email: {applied}.")
        if manual:
            parts.append(f"📌 Tracked {len(manual)} portal/notify job(s) — apply via the link: {manual}.")
        if tailored_notes:
            parts.append("CV: " + "; ".join(tailored_notes) + ".")
        if missing:
            parts.append(f"Unknown job id(s): {missing}.")
        summary = " ".join(parts) or "Nothing approved."
        # Confirm the application back to Calvin (his ask: notified in Telegram AND on /status).
        # Only when something actually happened, and never from a test (injected notifier).
        if applied or manual:
            self._notify(f"📨 Application update:\n{summary}")
        return CommandResult(text=summary,
                             data={"applied": applied, "manual": manual, "missing": missing})

    def _auto_tailor(self, job: dict[str, Any]) -> str:
        """Tailor the CV to this job on approval. Returns a short note for the confirmation.

        Best-effort: a tailoring failure (LLM down, no master CV) must never block the
        application -- _send_application falls back to the master CV. Only runs if there is a
        master CV to tailor from.
        """
        if job.get("cv_variant"):
            return "used your existing tailored CV"
        try:
            res = self.cv_tailor.tailor(job_id=int(job["id"]), company=job.get("company") or "")
            if res.ok and res.data.get("variant"):
                before = res.data.get("ats_before")
                after = res.data.get("ats_after")
                if before is not None and after is not None:
                    return f"tailored CV to the role (ATS {before}→{after}/100)"
                return "tailored CV to the role"
        except Exception:  # noqa: BLE001 - never block an application on tailoring
            log.exception("auto-tailor failed for job %s", job.get("id"))
        return "used your master CV (tailoring unavailable)"

    def _send_application(self, keeper: dict[str, Any]) -> bool:
        subject = f"Application: {keeper['title']}"
        attachments = []
        cv = keeper.get("cv_variant") or find_master_cv()
        if cv:
            attachments.append(str(cv))
        try:
            self.mailer.send_application(
                to=keeper["apply_target"], subject=subject, body=keeper["cover"],
                attachments=attachments,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to send application for job %s", keeper.get("id"))
            return False
        self.mem.record_application(
            job_id=keeper["id"], company=keeper.get("company"), source=keeper.get("source"),
            category=keeper.get("category"), cv_variant=keeper.get("cv_variant"),
            notes=f"emailed {keeper['apply_target']}" + ("" if cv else " (no CV attached — none on file)"),
        )
        return True

    # ------------------------------------------------------------- status / report
    def status(self, **_: Any) -> CommandResult:
        counts = {}
        for row in self.mem.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status"):
            counts[row["status"]] = row["c"]
        drafted = self.mem.jobs_by_status("drafted", limit=10) + self.mem.jobs_by_status("notified", limit=10)
        lines = [f"Jobs by status: {counts or 'none yet'}."]
        if drafted:
            lines.append("Awaiting approval:")
            for j in drafted[:10]:
                lines.append(f"  [{j['id']}] {j['title']} @ {j['company']} "
                             f"({j['score']}, {j['category']}, {j['apply_kind']})")
        return CommandResult(text="\n".join(lines), data={"counts": counts})

    def report(self, notify: bool = True, **_: Any) -> CommandResult:
        """Weekly Sunday report: applications, response rate, interviews, per-category."""
        since = time.time() - 7 * 86400
        stats = self.mem.application_stats(since)
        interviews = stats["by_status"].get("interview", 0)
        replied = stats["by_status"].get("replied", 0) + interviews + stats["by_status"].get("offer", 0)
        total = stats["total"]
        rate = f"{(replied / total * 100):.0f}%" if total else "n/a"
        cats = ", ".join(f"{k}:{v}" for k, v in stats["by_category"].items()) or "none"
        text = (
            f"📊 Weekly job report ({time.strftime('%d %b')})\n"
            f"Applications sent: {total}\n"
            f"Responses: {replied} (rate {rate}), interviews: {interviews}\n"
            f"By category: {cats}"
        )
        if notify:
            self._notify(text)
        return CommandResult(text=text, data=stats)

    # ------------------------------------------------------------- watch
    def watch(self, company: str = "", url: str = "", **_: Any) -> CommandResult:
        if not company or not url:
            return CommandResult(text="Usage: watch <company> <careers-url>.", ok=False)
        from skills.job_hunter.sources.watched import WatchedCompaniesSource

        WatchedCompaniesSource.add(company, url)
        return CommandResult(text=f"Now watching {company}'s careers page daily: {url}")

    # ------------------------------------------------------------- interview watcher
    def interview_check(self, messages: list[dict[str, str]] | None = None, **_: Any) -> CommandResult:
        """Match inbound mail against applied companies; alert on interview invites.

        `messages` (list of {gmail_id, sender, subject, snippet}) can be injected for tests;
        otherwise recent inbox metadata is pulled from Gmail. Degrades gracefully if unauthed.
        """
        companies = [c.lower() for c in self.mem.applied_company_names() if c]
        if not companies:
            return CommandResult(text="No applications yet — nothing to watch.", data={"alerts": 0})

        if messages is None:
            messages = self._recent_inbox_metadata()
        if messages is None:
            return CommandResult(text="Gmail not reachable — interview check skipped.", ok=False)

        alerted = set(json.loads(self.mem.kv_get("job_hunter.interview_alerted") or "[]"))
        alerts = 0
        for msg in messages:
            gid = msg.get("gmail_id", "")
            if gid in alerted:
                continue
            blob = f"{msg.get('sender','')} {msg.get('subject','')} {msg.get('snippet','')}".lower()
            if not any(c in blob for c in companies):
                continue
            label = self.llm.classify(
                f"From: {msg.get('sender')}\nSubject: {msg.get('subject')}\n{msg.get('snippet')}",
                ["interview_invite", "response", "unrelated"],
                instruction="Is this a job interview invitation, a general response to an application, or unrelated?",
            )
            if label == "interview_invite":
                matched = next((c for c in companies if c in blob), "")
                self._notify(f"🎯 Interview invite detected!\nFrom: {msg.get('sender')}\n"
                              f"Subject: {msg.get('subject')}\n\nGenerating your prep pack…")
                self._auto_prep(matched or msg.get("sender", ""))
                alerts += 1
            if label in ("interview_invite", "response"):
                alerted.add(gid)
        self.mem.kv_set("job_hunter.interview_alerted", json.dumps(sorted(alerted)))
        return CommandResult(text=f"Interview check done — {alerts} new invite(s).", data={"alerts": alerts})

    def _auto_prep(self, company: str) -> None:
        """Fire the Phase 6 prep pack for a detected interview invite (best-effort)."""
        try:
            self.prep.prep(company=company.title() if company else "the company")
        except Exception:  # noqa: BLE001 - prep failure must not break the watcher
            log.exception("auto prep generation failed for %s", company)

    def _recent_inbox_metadata(self) -> list[dict[str, str]] | None:
        try:
            from core.gmail_client import GmailClient

            gmail = GmailClient()
            ids = gmail.list_inbox(max_results=25, query="newer_than:2d")
            out = []
            for mid in ids:
                m = gmail.get_message(mid, fmt="metadata")
                out.append({"gmail_id": mid, "sender": gmail.header(m, "From"),
                            "subject": gmail.header(m, "Subject"), "snippet": m.get("snippet", "")})
            return out
        except Exception:  # noqa: BLE001
            log.warning("interview_check: Gmail unavailable")
            return None

    # ------------------------------------------------------------- digest render
    def _render_digest(self, keepers: list[dict[str, Any]], deferred: int, auto_applied: int,
                       queued: int = 0) -> str:
        if not keepers:
            return "Hunt complete — no postings cleared the score threshold this run."
        name = get_settings().my_name
        lines = [f"💼 {name}'s job digest — {len(keepers)} match(es)"]
        if not is_seeded(self.mem):
            lines.append("⚠️ Persona not seeded — covers are placeholders until persona-init (Phase 4).")
        for k in sorted(keepers, key=lambda x: x["score"], reverse=True):
            route = {"email": "✉️ email-apply", "portal": "🔗 apply on site",
                     "notify_only": "📝 portal signup"}.get(k["apply_kind"], k["apply_kind"])
            lines.append(
                f"\n[{k['id']}] {k['title']} @ {k['company']}  ({k['score']}/100 · {k['category']})\n"
                f"    {k['summary']}\n    {route}: {k['apply_target'] or k['url']}"
            )
        lines.append(f"\n➡️ Reply `approve {','.join(str(k['id']) for k in keepers[:3])}` to apply, "
                     "or use the Telegram buttons (Phase 8).")
        if auto_applied:
            lines.append(f"🤖 AUTO_APPLY sent {auto_applied} email application(s) automatically.")
        if queued:
            lines.append(f"(⚙️ {queued} more posting(s) queued — workers are scoring them now.)")
        if deferred:
            lines.append(f"({deferred} more new posting(s) deferred to the next run.)")
        return "\n".join(lines)


@handler("job_hunter.score_one")
def score_one(job_id: int) -> str:
    """Score+draft ONE already-scraped job. The unit of work the queue drains.

    Module-level and resolved by name so a worker running a newer image can still drain rows
    enqueued by an older one -- the queue stores a string, never a pickled callable.
    """
    from skills.job_hunter import SKILL as hunter

    row = hunter.mem.get_job(int(job_id))
    if row is None:
        return f"job {job_id} no longer exists"
    if row["status"] not in ("new", None):
        return f"job {job_id} already {row['status']}"
    raw = RawJob.from_dict(json.loads(row["raw_json"])) if row.get("raw_json") else None
    if raw is None:
        return f"job {job_id} has no raw payload to score"
    keeper = hunter._score_and_draft(int(job_id), raw)
    return f"scored {job_id}" + (" (keeper)" if keeper else " (below threshold)")


def find_master_cv() -> Path | None:
    """Locate data/cv/master_cv.* if present (populated in Phase 15)."""
    cv_dir = get_settings().data_dir / "cv"
    if not cv_dir.exists():
        return None
    for p in sorted(cv_dir.glob("master_cv.*")):
        return p
    return None


SKILL = JobHunterSkill()
