"""Semester command center (Phase 13).

The integrative hub. A `deadlines` table (from the email watcher — extracted dates are
confirmed before saving — plus /deadline add and voice) and config/timetable.yaml drive:
  * a unified 07:00 EAT morning briefing that REPLACES the plain inbox summary — today's
    classes, deadlines within 7 days ranked by urgency×weight, flashcards due, freelance/
    volunteer commitments, pending job approvals, interviews, and upcoming events, ending
    with a ruthless suggested top-3;
  * /plan — a Sunday week planner biased to the weakest units (Phase 11 data) and nearest
    deadlines, which Calvin edits and the briefings then reference;
  * /cram <unit> — panic mode: surge the unit's weak flashcards, build a compressed revision
    schedule from vault coverage, and generate a fresh MUST-format mock CAT (PDF), with the
    marking scheme delivered separately only after he attempts it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from core.config import get_settings
from core import timetable
from core.llm import LLMClient, LLMError, get_client
from core.expiry import JobExpiry
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.pdf import build_pdf
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.time_context import (format_local, greeting, local_now, parse_local_datetime,
                               relative_due)

log = get_logger("skills.semester_planner")

_PLAN_KEY = "semester.plan"
_CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.I)
# Mirrors job_hunter's staleness rule (Phase 34): overdue this long is no longer "upcoming",
# it's just noise crowding out deadlines that ARE still ahead of him.
_STALE_OVERDUE_DAYS = 3
_MAX_OVERDUE_SHOWN = 2


class SemesterPlannerSkill(BaseSkill):
    name = "semester_planner"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 clock: Callable[[], float] = time.time,
                 notify: Callable[[str], bool] | None = None,
                 weather: Any | None = None) -> None:
        self._mem = memory
        self._llm = llm
        self._now = clock
        # Injectable so a test can never reach Calvin's phone. It wasn't, and
        # extract_deadlines() notifies unconditionally, so every suite run texted him
        # "I found 1 possible deadline(s) in your email" about a fixture.
        self._notify = notify or send_telegram
        self._weather = weather

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def weather(self) -> Any:
        if self._weather is None:
            from skills.weather import SKILL as WEATHER_SKILL

            self._weather = WEATHER_SKILL
        return self._weather

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    def contract(self) -> SkillContract:
        """Reads `study` and `notifications` — it owns the 07:00 briefing, which is the one
        message Calvin reads every day, so "don't wake me before 8" has to be able to reach it.

        `never_invents_a_deadline` matters more here than anywhere: the briefing is where a
        hallucinated CAT date would look most official. Dates extracted from email stay
        `pending` until he confirms them.
        """
        return SkillContract(reads_categories=["study", "notifications"],
                             hard_invariants=["never_invents_a_deadline"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "briefing": self.briefing, "due": self.due, "plan": self.plan, "cram": self.cram,
            "deadline_add": self.deadline_add, "confirm_deadline": self.confirm_deadline,
            "reject_deadline": self.reject_deadline, "extract_deadlines": self.extract_deadlines,
            "cram_marking": self.cram_marking,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        hour = int(get_settings().get("planner", "briefing_hour", default=7))
        return [
            ScheduledJob(id="planner.briefing", func=self.briefing, trigger="cron",
                         kwargs={"hour": hour, "minute": 0}),
            ScheduledJob(id="planner.weekplan", func=self.plan, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 17}),
        ]

    # ------------------------------------------------------------- deadlines
    def deadline_add(self, title: str = "", due: str = "", unit: str = "", dtype: str = "",
                     weight: float = 1.0, **_: Any) -> CommandResult:
        if not title or not due:
            return CommandResult(text="Usage: deadline add <title> <YYYY-MM-DD> [unit] [type].", ok=False)
        epoch = _iso_to_epoch(due)
        if epoch is None:
            return CommandResult(text=f"Couldn't parse date '{due}' (use YYYY-MM-DD).", ok=False)
        deadline_id = self.mem.add_deadline(
            title, epoch, unit=unit or None, dtype=dtype or None,
            weight=float(weight), status="active", source="manual",
        )
        return CommandResult(
            text=(f"📌 Saved deadline\n"
                  f"• {title} · {unit or 'general'}\n"
                  f"• {format_local(epoch)} ({relative_due(epoch, self._now())})"),
            data={"deadline_id": deadline_id},
        )

    def due(self, days: int = 7, **_: Any) -> CommandResult:
        self._retire_stale_deadlines()
        rows = self._ranked_deadlines(days)
        if not rows:
            return CommandResult(text=f"Nothing due in the next {days} days. 🎉", data={"count": 0})
        lines = [f"🗓 DEADLINES · next {days} days", ""]
        for d, score, dleft in rows:
            lines.append(
                f"• {d['title']}\n"
                f"  {d['unit'] or 'general'} · {d['type'] or 'task'} · "
                f"{format_local(d['due_at'])}\n"
                f"  {relative_due(d['due_at'], self._now())} · priority {score:.1f}"
            )
        return CommandResult(text="\n\n".join(lines), data={"count": len(rows),
                             "generated_at": local_now(self._now()).isoformat()})

    def _retire_stale_deadlines(self) -> int:
        """Auto-retire deadlines that no longer belong in "next 7 days": a CANCELLED event
        (event_scout.interested() adds a deadline row for the registration/attendance date;
        nothing ever retired it when the event itself was later cancelled — the log showed
        the exact same 5 stale rows, including one visibly titled "... - CANCELLED", crowding
        out the "next 7 days" list for four briefings running) and anything overdue long
        enough that it clearly isn't happening. Nothing is deleted (§0 P4) — status moves to
        'cancelled'/'expired' and `_ranked_deadlines`'s `status='active'` filter does the rest.
        """
        now = self._now()
        retired = 0
        rows = self.mem.execute(
            "SELECT id, title, due_at FROM deadlines WHERE status='active'").fetchall()
        for d in rows:
            if _CANCELLED_RE.search(d["title"] or ""):
                self.mem.set_deadline_status(d["id"], "cancelled")
                retired += 1
            elif d["due_at"] < now - _STALE_OVERDUE_DAYS * 86400:
                self.mem.set_deadline_status(d["id"], "expired")
                retired += 1
        return retired

    def _ranked_deadlines(self, days: int) -> list[tuple[Any, float, float]]:
        now = self._now()
        out = []
        rows = self.mem.execute(
            "SELECT * FROM deadlines WHERE status='active' AND due_at<=%s ORDER BY due_at",
            (now + days * 86400,),
        ).fetchall()
        for d in rows:
            days_left = (d["due_at"] - now) / 86400
            score = d["weight"] / max(0.5, days_left)   # urgency × weight
            if days_left < 0:
                score = 10_000 + d["weight"] + abs(days_left)
            out.append((d, score, days_left))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def confirm_deadline(self, deadline_id: int | str = 0, **_: Any) -> CommandResult:
        self.mem.set_deadline_status(int(deadline_id), "active")
        return CommandResult(text=f"✅ Deadline {deadline_id} confirmed and saved.")

    def reject_deadline(self, deadline_id: int | str = 0, **_: Any) -> CommandResult:
        self.mem.set_deadline_status(int(deadline_id), "cancelled")
        return CommandResult(text=f"Deadline {deadline_id} discarded.")

    def extract_deadlines(self, messages: list[dict[str, str]] | None = None, **_: Any) -> CommandResult:
        """Email watcher: pull deadline dates from school mail -> save as PENDING for confirmation."""
        if messages is None:
            messages = self._recent_school_mail()
        if not messages:
            return CommandResult(text="No school mail to scan.", data={"pending": 0})
        pending = 0
        for msg in messages:
            try:
                data = self.llm.chat_json(
                    "classify",
                    [{"role": "system", "content":
                        "Extract academic deadlines from this email. Return JSON with deadlines: each "
                        "{title, unit, type(CAT/assignment/exam/lab), due_date(YYYY-MM-DD), weight}. "
                        "Empty list if none. Do not invent dates."},
                     {"role": "user", "content":
                        f"From: {msg.get('sender')}\nSubject: {msg.get('subject')}\n{msg.get('snippet')}"}],
                    schema_hint='{"deadlines": [{"title": string, "unit": string, "type": string, '
                                '"due_date": string, "weight": number}]}',
                    temperature=0.0, max_tokens=400)
            except LLMError:
                continue
            for d in data.get("deadlines", []):
                epoch = _iso_to_epoch(d.get("due_date", ""))
                if epoch and d.get("title"):
                    self.mem.add_deadline(d["title"], epoch, unit=d.get("unit") or None,
                                          dtype=d.get("type") or None, weight=float(d.get("weight", 1.0)),
                                          status="pending", source="email")
                    pending += 1
        if pending:
            self._notify(f"🗓 I found {pending} possible deadline(s) in your email — confirm them with /deadlines.")
        return CommandResult(text=f"Extracted {pending} pending deadline(s) — awaiting your confirmation.",
                             data={"pending": pending})

    def _recent_school_mail(self) -> list[dict[str, str]]:
        rows = self.mem.execute(
            "SELECT gmail_id, sender, subject FROM emails WHERE category IN ('important','job_related') "
            "ORDER BY processed_at DESC LIMIT 15").fetchall()
        return [{"sender": r["sender"], "subject": r["subject"], "snippet": ""} for r in rows]

    # ------------------------------------------------------------- events (#21)
    def _upcoming_interested_events(self, now: float) -> list[dict[str, Any]]:
        """The 3 soonest events he's marked interested in that haven't already happened.

        The old query ("ORDER BY id DESC LIMIT 3") never checked the date at all, so once
        the top 3 most-recently-marked events had all passed, the briefing kept showing the
        same three stale titles every single day -- "static", not "upcoming". event_scout
        never flips status off "interested" on its own (there's no reason it should; that
        would erase a record of what he cared about), so the filtering has to happen here,
        at read time, the same way event_scout's own _ranked() drops past events.
        """
        from skills.event_scout.sources import parse_event_date

        rows = self.mem.execute(
            "SELECT title, date FROM events WHERE status='interested' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        dated = []
        for r in rows:
            epoch = parse_event_date(r["date"])
            if epoch and epoch < now - 86400:   # already happened; same grace window as _ranked()
                continue
            dated.append((epoch if epoch is not None else float("inf"), r))
        dated.sort(key=lambda pair: pair[0])
        return [r for _epoch, r in dated[:3]]

    # ------------------------------------------------------------- morning briefing
    def briefing(self, notify: bool = True, **_: Any) -> CommandResult:
        """The unified 07:00 briefing (replaces the plain inbox summary)."""
        now = self._now()
        local = local_now(now)
        date_iso = local.strftime("%Y-%m-%d")
        names = timetable.unit_names()

        classes = timetable.classes_on(local.weekday(), date_iso)
        self._retire_stale_deadlines()
        ranked = self._ranked_deadlines(7)
        cards_due = len(self.mem.due_cards(now=now))
        job_approvals = self.mem.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status IN ('drafted','notified')").fetchone()["c"]
        interviews = self.mem.execute(
            "SELECT company FROM applications WHERE status='interview'").fetchall()
        events = self._upcoming_interested_events(now)
        commitments = get_settings().get("planner", "commitments", default=[]) or []

        name = get_settings().my_name
        lines = [
            f"📋 DAILY BRIEFING · {local.strftime('%A %d %b')}",
            f"{greeting(now)}, {name}. Local time is {local.strftime('%H:%M %Z')}.",
        ]

        weather_line = self._weather_line()
        if weather_line:
            lines.append(weather_line)

        lines.append("\n📚 Classes today: " + (
            ", ".join(f"{c.get('start','')} {c.get('title', c.get('unit',''))}" for c in classes)
            if classes else "none"))

        if ranked:
            # Overdue and upcoming rendered as separate sections, overdue capped: four stale
            # overdue rows used to fill every one of the "next 7 days" slots, making genuinely
            # upcoming deadlines invisible even after _retire_stale_deadlines() cleans out the
            # oldest ones. A handful of days overdue can still be legitimately worth seeing;
            # burying the week ahead under all of them is the actual problem.
            overdue = [(d, s, dl) for d, s, dl in ranked if dl < 0]
            upcoming = [(d, s, dl) for d, s, dl in ranked if dl >= 0]
            if overdue:
                lines.append("\n⚠️ Overdue:")
                for d, _s, _dl in overdue[:_MAX_OVERDUE_SHOWN]:
                    lines.append(
                        f"  • {d['title']} ({names.get(d['unit'], d['unit'] or 'general')}) "
                        f"— {relative_due(d['due_at'], now)}"
                    )
                if len(overdue) > _MAX_OVERDUE_SHOWN:
                    lines.append(f"  (+{len(overdue) - _MAX_OVERDUE_SHOWN} more overdue)")
            if upcoming:
                lines.append("\n🗓 Deadlines (next 7 days):")
                for d, _s, dleft in upcoming[:5]:
                    lines.append(
                        f"  • {d['title']} ({names.get(d['unit'], d['unit'] or 'general')}) "
                        f"— {relative_due(d['due_at'], now)} · {format_local(d['due_at'])}"
                    )
        lines.append(f"\n🧠 Flashcards due: {cards_due}   ·   💼 Job approvals pending: {job_approvals}")

        # Applications whose window is about to shut. Named individually, not counted: a
        # number tells him something is urgent without telling him what to do about it.
        closing = JobExpiry(self.mem).upcoming_deadlines(within_days=3, now=now)
        if closing:
            lines.append("\n⏳ Applications closing soon:")
            for job in closing[:5]:
                where = f" at {job['company']}" if job.get("company") else ""
                lines.append(f"  • {job['title']}{where} — closes "
                             f"{relative_due(job['deadline'], now)} · "
                             f"{format_local(job['deadline'])}  (job #{job['id']})")
        if interviews:
            lines.append("🎯 Interviews: " + ", ".join(i["company"] for i in interviews))
        if events:
            lines.append("📅 Events: " + ", ".join(e["title"] for e in events))
        if commitments:
            lines.append("🔧 Commitments: " + "; ".join(commitments))

        top3 = self._top3(ranked, cards_due, job_approvals, classes, commitments)
        lines.append(f"\n🎯 Top 3 today:\n{top3}")

        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={
            "classes": len(classes), "deadlines": len(ranked), "cards_due": cards_due,
            "job_approvals": job_approvals, "local_time": local.isoformat(),
            "timezone": get_settings().tz})

    def _weather_line(self) -> str:
        """Weather (Phase 36) belongs IN the briefing, not as a standalone thing nobody
        checks. Never lets a weather-service hiccup take the whole briefing down with it —
        WeatherSkill.current() already degrades to an honest message rather than raising,
        but a network stack can still surprise you, so this is a second line of defence."""
        try:
            return f"\n🌦 {self.weather.current().text}"
        except Exception:  # noqa: BLE001 - weather must never break the briefing
            return ""

    def _top3(self, ranked, cards_due: int, job_approvals: int, classes, commitments) -> str:
        # `dl` (days_left, from _ranked_deadlines) is correctly SIGNED — negative once overdue.
        # A bare f"in {dl:.0f}d" throws that sign into a string the LLM then has to interpret,
        # and a model asked to write fluent prose about "in -2d" smooths it into confident,
        # wrong text ("due in 3 days") rather than preserving the overdue status. Passing the
        # same relative_due() string already used in the Deadlines section above removes the
        # ambiguity entirely — there is no signed number left for either the LLM or the
        # LLMError fallback below to misread.
        summary = {
            "top_deadlines": [f"{d['title']} — {relative_due(d['due_at'], self._now())} (w{d['weight']})"
                              for d, _s, _dl in ranked[:4]],
            "cards_due": cards_due, "job_approvals": job_approvals,
            "classes": [c.get("title") for c in classes], "commitments": commitments,
        }
        try:
            return self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "You are a ruthless prioritizer. From this student's day, output EXACTLY 3 numbered, "
                    "specific, high-leverage actions — most important first. Each deadline is already "
                    "labelled OVERDUE or due-in-N — preserve that status verbatim, never invert or "
                    "soften it. No preamble."},
                 {"role": "user", "content": json.dumps(summary)}], max_tokens=200).strip()
        except LLMError:
            picks = []
            if ranked:
                status = relative_due(ranked[0][0]["due_at"], self._now())
                picks.append(f"1. {'Finish' if ranked[0][1] >= 10_000 else 'Start'} "
                             f"'{ranked[0][0]['title']}' ({status})")
            if cards_due:
                picks.append(f"{len(picks)+1}. Clear {cards_due} due flashcards")
            if job_approvals:
                picks.append(f"{len(picks)+1}. Review {job_approvals} job application(s)")
            return "\n".join(picks[:3]) or "1. Review your goals for the week."

    # ------------------------------------------------------------- week planner
    def _weekly_schedule_summary(self) -> list[str]:
        """Human-readable day-by-day class times. `plan()` used to hand the model only the
        list of unit CODES (`timetable.unit_names().keys()`) -- which units exist, never
        when they actually happen — so it had no way to "leave room for classes" the way its
        own system prompt claimed, and the same four generic blocks repeated on every day of
        the week including ones with classes on them.
        """
        data = timetable.load()
        names = timetable.unit_names(data)
        lines: list[str] = []
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
            for block in (data.get("weekly") or {}).get(day) or []:
                title = block.get("title") or names.get(block.get("unit"), block.get("unit") or "class")
                lines.append(f"{day.capitalize()} {block.get('start', '')}"
                             f"-{block.get('end', '')}: {title}")
        return lines

    def plan(self, notify: bool = True, **_: Any) -> CommandResult:
        """Propose a week plan biased to weakest units + nearest deadlines. Calvin edits & saves."""
        weakest = self.mem.weakest_cards(limit=8)
        weak_units = sorted({w["unit"] for w in weakest if w["unit"]})
        ranked = self._ranked_deadlines(14)
        commitments = get_settings().get("planner", "commitments", default=[]) or []
        payload = {
            "weak_units": weak_units,
            "deadlines": [f"{d['title']} ({d['unit']}) in {dl:.0f}d" for d, _s, dl in ranked[:8]],
            "commitments": commitments,
            "weekly_classes": self._weekly_schedule_summary(),
        }
        try:
            proposal = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "Propose a realistic 7-day study/work plan as day-by-day time blocks. Bias study time "
                    "to the weak units and nearest deadlines; leave room for classes, freelance/volunteer "
                    "commitments, and job-hunt admin. Concise, editable."},
                 {"role": "user", "content": json.dumps(payload)}], max_tokens=900).strip()
        except LLMError:
            proposal = "Couldn't generate a plan right now."
        self.mem.kv_set(_PLAN_KEY, proposal)
        if notify:
            self._notify("🗂 Proposed week plan (edit anytime):\n\n" + proposal)
        return CommandResult(text=proposal, data={"weak_units": weak_units})

    # ------------------------------------------------------------- cram / panic mode
    def cram(self, unit: str = "", days: int = 5, notify: bool = True, **_: Any) -> CommandResult:
        """Exam-approaching cram: surge weak cards, build a revision schedule + a mock CAT PDF."""
        if not unit:
            return CommandResult(text="Usage: cram <unit> — I'll build a revision plan + mock CAT.", ok=False)
        surged = self.mem.surge_unit(unit, now=self._now())
        chunks = self.mem.vault_chunks(unit)
        weak = self.mem.weakest_cards(unit=unit, limit=10)
        coverage = "\n".join(c["text"][:400] for c in chunks[:6]) or "(no vault notes for this unit yet)"

        cat = self._mock_cat(unit, coverage)
        paper_pdf, scheme_pdf = self._write_cat_pdfs(unit, cat)
        schedule = self._revision_schedule(unit, days, coverage, [w["front"] for w in weak])

        msg = (f"🚨 Cram mode: {unit} ({days} days)\n"
               f"Surged {surged} weak card(s) to due-now.\n\n"
               f"Revision schedule:\n{schedule}\n\n"
               f"📄 Mock CAT (MUST format): {paper_pdf.name}\n"
               f"(Attempt it first — the marking scheme unlocks via /cram_marking {unit}.)")
        if notify:
            self._notify(msg)
        return CommandResult(text=msg, data={"surged": surged, "cat_pdf": str(paper_pdf),
                                             "marking_scheme": str(scheme_pdf), "cards": len(weak)})

    def _mock_cat(self, unit: str, coverage: str) -> dict[str, Any]:
        try:
            return self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Write a mock CAT (continuous assessment test) from these course notes: 4-5 questions "
                    "with marks totalling 30. For each include the model answer (for the marking scheme). "
                    "Practice only. Return JSON."},
                 {"role": "user", "content": f"Unit: {unit}\nNotes:\n{coverage[:3000]}"}],
                schema_hint='{"questions": [{"q": string, "marks": int, "answer": string}], "total": int}',
                temperature=0.4, max_tokens=1500)
        except LLMError:
            return {"questions": [], "total": 0}

    def _write_cat_pdfs(self, unit: str, cat: dict[str, Any]) -> tuple[Path, Path]:
        stamp = time.strftime("%Y%m%d", time.localtime(self._now()))
        base = get_settings().data_dir / "cram"
        names = timetable.unit_names()
        header = ["Meru University of Science and Technology",
                  f"{unit} — {names.get(unit, '')}", "MOCK CAT (practice)",
                  "Time: 1 hour  ·  Answer ALL questions.", ""]
        q_paras, s_paras = list(header), list(header[:3]) + ["MARKING SCHEME", ""]
        for i, q in enumerate(cat.get("questions", []), start=1):
            q_paras.append(f"{i}. {q.get('q','')}  [{q.get('marks','?')} marks]")
            s_paras.append(f"{i}. {q.get('q','')}  [{q.get('marks','?')} marks]")
            s_paras.append(f"   Model answer: {q.get('answer','')}")
        paper = build_pdf(base / f"{unit}_CAT_{stamp}.pdf", f"{unit} Mock CAT",
                          [("", q_paras or ["(no questions)"])])
        scheme = build_pdf(base / f"{unit}_CAT_{stamp}_marking.pdf", f"{unit} Mock CAT — Marking Scheme",
                           [("", s_paras)])
        return paper, scheme

    def _revision_schedule(self, unit: str, days: int, coverage: str, weak_fronts: list[str]) -> str:
        try:
            return self.llm.chat(
                "write",
                [{"role": "system", "content":
                    f"Build a compressed {days}-day revision schedule for this unit's exam, front-loading "
                    "the student's weak topics. Day-by-day, concise."},
                 {"role": "user", "content":
                    f"Unit: {unit}\nWeak topics: {weak_fronts}\nCoverage sample:\n{coverage[:1500]}"}],
                max_tokens=500).strip()
        except LLMError:
            return f"Days 1-{days}: revise weak topics ({', '.join(weak_fronts[:5])}), then full past-paper."

    def cram_marking(self, unit: str = "", **_: Any) -> CommandResult:
        """Reveal the most recent mock-CAT marking scheme for a unit (after attempting)."""
        base = get_settings().data_dir / "cram"
        schemes = sorted(base.glob(f"{unit}_CAT_*_marking.pdf")) if base.exists() else []
        if not schemes:
            return CommandResult(text=f"No mock CAT found for {unit}. Run /cram {unit} first.", ok=False)
        return CommandResult(text=f"📄 Marking scheme: {schemes[-1].name} — grade yourself honestly.",
                             data={"marking_scheme": str(schemes[-1])})


def _iso_to_epoch(iso: str) -> float | None:
    """Parse in the configured user timezone; bare dates remain due through 23:59:59."""
    return parse_local_datetime(iso, date_at_end_of_day=True)


SKILL = SemesterPlannerSkill()
