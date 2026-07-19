"""Spaced repetition engine (Phase 11).

SM-2 over the flashcards table (no external service). Candidate cards from the lecture
pipeline and vault are approved/edited before entering rotation; then daily/on-demand
review ("quiz me", /quiz [unit]) runs a Telegram or voice session. Telegram grading uses
Again/Hard/Good/Easy; voice mode has the model judge the spoken answer (lenient on phrasing,
strict on substance). A weekly report surfaces retention + weakest topics, and surge()
brings a unit's weak cards forward when an exam is near.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.sm2 import CardState, GRADES, schedule
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.spaced_rep")

_SESSION_KEY = "spaced_rep.session"


class SpacedRepSkill(BaseSkill):
    name = "spaced_rep"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 clock: Callable[[], float] = time.time,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._mem = memory
        self._llm = llm
        self._now = clock
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    def contract(self) -> SkillContract:
        """Reads `study` (what and how he wants to revise) and `notifications` (when to quiz).

        `never_auto_activates_cards` is the rule the candidate/active split exists to enforce:
        a card generated from a lecture or the vault waits for him to approve it, so a
        mis-transcribed definition never becomes something he drills for weeks.
        """
        return SkillContract(reads_categories=["study", "notifications"],
                             hard_invariants=["never_auto_activates_cards"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "quiz": self.quiz, "reveal": self.reveal, "grade": self.grade,
            "quiz_answer": self.quiz_answer,
            "approve_card": self.approve_card, "reject_card": self.reject_card,
            "edit_card": self.edit_card, "add_card": self.add_card,
            "candidates": self.list_candidates, "report": self.report, "surge": self.surge,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        hour = int(get_settings().get("srs", "daily_hour", default=18))
        return [
            ScheduledJob(id="srs.daily", func=self.daily_reminder, trigger="cron",
                         kwargs={"hour": hour, "minute": 0}),
            ScheduledJob(id="srs.report", func=self.report, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 19}),
        ]

    def session_active(self) -> bool:
        return bool(self.mem.kv_get(_SESSION_KEY))

    # ------------------------------------------------------------- quiz session
    def quiz(self, unit: str = "", **_: Any) -> CommandResult:
        """Start a review session over cards due now (optionally for one unit)."""
        due = self.mem.due_cards(unit or None, now=self._now())
        if not due:
            n_cand = self.mem.count_flashcards(unit or None, status="candidate")
            extra = f" ({n_cand} candidate card(s) await approval — /cards)" if n_cand else ""
            return CommandResult(text=f"No cards due right now.{extra}", data={"due": 0})
        session = {"unit": unit, "ids": [c["id"] for c in due], "idx": 0, "revealed": False,
                   "graded": 0}
        self.mem.kv_set(_SESSION_KEY, json.dumps(session))
        card = self.mem.get_flashcard(session["ids"][0])
        return CommandResult(
            text=f"🧠 Quiz — {len(due)} card(s) due.\n\nQ1. {card['front']}",
            data={"card_id": card["id"], "front": card["front"], "index": 0, "total": len(due)})

    def _load(self) -> dict[str, Any] | None:
        raw = self.mem.kv_get(_SESSION_KEY)
        return json.loads(raw) if raw else None

    def reveal(self, **_: Any) -> CommandResult:
        s = self._load()
        if not s:
            return CommandResult(text="No quiz in progress. Say 'quiz me' or /quiz.", ok=False)
        card = self.mem.get_flashcard(s["ids"][s["idx"]])
        s["revealed"] = True
        self.mem.kv_set(_SESSION_KEY, json.dumps(s))
        return CommandResult(text=f"A: {card['back']}\n\nGrade it: Again / Hard / Good / Easy.",
                             data={"card_id": card["id"], "back": card["back"]})

    def grade(self, grade: str = "", **_: Any) -> CommandResult:
        """Apply an SM-2 grade to the current card and advance."""
        s = self._load()
        if not s:
            return CommandResult(text="No quiz in progress.", ok=False)
        grade = (grade or "").strip().lower()
        if grade not in GRADES:
            return CommandResult(text=f"Grade must be one of {', '.join(GRADES)}.", ok=False)
        self._apply_grade(s["ids"][s["idx"]], s.get("unit") or None, grade)
        s["graded"] += 1
        return self._advance(s)

    def quiz_answer(self, answer: str = "", **_: Any) -> CommandResult:
        """Voice/typed mode: judge the answer, auto-grade, give feedback, advance."""
        s = self._load()
        if not s:
            return CommandResult(text="No quiz in progress.", ok=False)
        card = self.mem.get_flashcard(s["ids"][s["idx"]])
        grade, feedback = self._judge(card["front"], card["back"], answer)
        self._apply_grade(card["id"], s.get("unit") or None, grade)
        s["graded"] += 1
        nxt = self._advance(s)
        return CommandResult(text=f"{feedback}\n(correct answer: {card['back']})\n\n{nxt.text}",
                             data={**nxt.data, "judged_grade": grade})

    def _judge(self, front: str, back: str, answer: str) -> tuple[str, str]:
        if not answer.strip():
            return "again", "No answer given."
        try:
            data = self.llm.chat_json(
                "classify",
                [{"role": "system", "content":
                    "Judge a flashcard answer. Be lenient on phrasing/wording, strict on substance. "
                    "Return JSON with grade in [again,hard,good,easy] and one-line feedback."},
                 {"role": "user", "content": f"Q: {front}\nCorrect: {back}\nStudent: {answer}"}],
                schema_hint='{"grade": string, "feedback": string}', temperature=0.0, max_tokens=120)
            grade = str(data.get("grade", "good")).lower()
            if grade not in GRADES:
                grade = "good"
            return grade, str(data.get("feedback", ""))
        except LLMError:
            return "good", "(couldn't judge automatically — grade yourself)"

    def _apply_grade(self, card_id: int, unit: str | None, grade: str) -> None:
        card = self.mem.get_flashcard(card_id)
        state = CardState(ease=card["ease"], interval_days=card["interval_days"], lapses=card["lapses"])
        nxt = schedule(state, grade)
        due_at = self._now() + nxt.interval_days * 86400
        self.mem.update_card_schedule(card_id, ease=nxt.ease, interval_days=nxt.interval_days,
                                      lapses=nxt.lapses, due_at=due_at)
        self.mem.log_review(card_id, unit, grade, now=self._now())

    def _advance(self, s: dict[str, Any]) -> CommandResult:
        s["idx"] += 1
        s["revealed"] = False
        if s["idx"] >= len(s["ids"]):
            self.mem.kv_set(_SESSION_KEY, "")
            return CommandResult(text=f"✅ Done — {s['graded']} card(s) reviewed. Nice work.",
                                 data={"done": True, "graded": s["graded"]})
        self.mem.kv_set(_SESSION_KEY, json.dumps(s))
        card = self.mem.get_flashcard(s["ids"][s["idx"]])
        return CommandResult(text=f"Q{s['idx'] + 1}. {card['front']}",
                             data={"done": False, "card_id": card["id"], "front": card["front"],
                                   "index": s["idx"]})

    # ------------------------------------------------------------- card management
    def list_candidates(self, unit: str = "", **_: Any) -> CommandResult:
        cands = self.mem.candidate_cards(unit or None)
        if not cands:
            return CommandResult(text="No candidate cards awaiting approval.", data={"candidates": []})
        lines = [f"{len(cands)} candidate card(s) — approve to add to rotation:"]
        for c in cands[:20]:
            lines.append(f"  [{c['id']}] ({c['unit']}) {c['front']} → {c['back']}")
        return CommandResult(text="\n".join(lines),
                             data={"candidates": [{"id": c["id"], "front": c["front"],
                                                   "back": c["back"], "unit": c["unit"]} for c in cands]})

    def approve_card(self, card_id: int | str = 0, **_: Any) -> CommandResult:
        self.mem.approve_card(int(card_id), now=self._now())
        return CommandResult(text=f"✅ Card {card_id} added to your review rotation.")

    def reject_card(self, card_id: int | str = 0, **_: Any) -> CommandResult:
        self.mem.reject_card(int(card_id))
        return CommandResult(text=f"Card {card_id} suspended (kept, not deleted).")

    def edit_card(self, card_id: int | str = 0, front: str = "", back: str = "", **_: Any) -> CommandResult:
        self.mem.edit_card(int(card_id), front=front or None, back=back or None)
        return CommandResult(text=f"Card {card_id} updated.")

    def add_card(self, front: str = "", back: str = "", unit: str = "", **_: Any) -> CommandResult:
        if not front or not back:
            return CommandResult(text="Usage: add_card front / back.", ok=False)
        added = self.mem.add_flashcard(front, back, unit=unit or None, source="manual", status="candidate")
        return CommandResult(text="Card added (as candidate — approve with /cards)." if added
                             else "That card already exists.", data={"added": added})

    # ------------------------------------------------------------- report / surge / reminders
    def report(self, notify: bool = True, **_: Any) -> CommandResult:
        since = self._now() - 7 * 86400
        stats = self.mem.retention_stats(since)
        weak = self.mem.weakest_cards(limit=5)
        due_now = len(self.mem.due_cards(now=self._now()))
        lines = [f"📈 Weekly review report ({time.strftime('%d %b')})", f"Cards due now: {due_now}"]
        if stats:
            for unit, s in stats.items():
                lines.append(f"  {unit}: {s['reviews']} reviews, retention {int(s['retention']*100)}%")
        else:
            lines.append("  No reviews logged this week.")
        if weak:
            lines.append("Weakest cards:")
            lines.extend(f"  • ({w['unit']}) {w['front']} [lapses {w['lapses']}]" for w in weak)
        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={"stats": stats, "due_now": due_now})

    def surge(self, unit: str = "", **_: Any) -> CommandResult:
        """Bring a unit's weak cards forward (exam approaching). Ties into the Phase 13 planner."""
        if not unit:
            return CommandResult(text="Usage: surge <unit> — surfaces that unit's weak cards now.", ok=False)
        n = self.mem.surge_unit(unit, now=self._now())
        return CommandResult(text=f"Surged {n} weak card(s) in {unit} to due-now for cramming.",
                             data={"surged": n})

    def daily_reminder(self, **_: Any) -> CommandResult:
        due = len(self.mem.due_cards(now=self._now()))
        cand = self.mem.count_flashcards(status="candidate")
        if not due and not cand:
            return CommandResult(text="No cards due today.", data={"due": 0})
        msg = f"🧠 {due} flashcard(s) due today." + (f" {cand} candidate(s) to approve." if cand else "")
        msg += " Say 'quiz me' or /quiz to start."
        self._notify(msg)
        return CommandResult(text=msg, data={"due": due, "candidates": cand})

    # ------------------------------------------------------------- generate from vault
    def generate_from_vault(self, unit: str = "", max_cards: int = 10, **_: Any) -> CommandResult:
        """Auto-generate candidate cards from a unit's vault chunks (on demand, approval-gated)."""
        chunks = self.mem.vault_chunks(unit or None)
        if not chunks:
            return CommandResult(text="No vault content for that unit yet.", ok=False)
        sample = "\n\n".join(c["text"][:600] for c in chunks[:8])
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Create up to N concise flashcards (front question, back answer) from these course "
                    "notes. Only use facts present in the notes. Return JSON."},
                 {"role": "user", "content": f"N={max_cards}\n\nNOTES:\n{sample}"}],
                schema_hint='{"flashcards": [{"front": string, "back": string}]}',
                temperature=0.3, max_tokens=1200)
        except LLMError:
            return CommandResult(text="Couldn't generate cards right now.", ok=False)
        added = 0
        for c in data.get("flashcards", [])[:max_cards]:
            if c.get("front") and c.get("back") and self.mem.add_flashcard(
                    c["front"].strip(), c["back"].strip(), unit=unit or None, source="vault",
                    status="candidate"):
                added += 1
        return CommandResult(text=f"Generated {added} candidate card(s) from your {unit} notes — /cards to review.",
                             data={"added": added})


SKILL = SpacedRepSkill()
