"""Form & question-interview assistant (Phase 5).

Parses an application form or async screener (pasted text, URL, or forwarded email),
then answers each question strictly from the verified persona KB via persona.answer():
  * factual questions -> grounded answer, or flagged to Calvin when unknown (never guessed);
  * behavioral "tell us about a time…" -> pulled from the STAR story bank, or flagged to
    build one (2-3 quick questions) if none matches;
  * skills tests / coding assessments -> NEVER auto-solved — routed to a Phase 6 prep pack.
The result is a numbered answer sheet for Calvin to review/edit. Nothing is ever submitted
without his approval (§0); submit() is a separate, explicitly-invoked, approval-gated action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.mailer import ApplicationMailer
from core.persona_store import PersonaEngine, get_engine
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.form_assist")

# question types
SHORT, LONG, STORY, ASSESSMENT, CHOICE = "short", "long", "story", "assessment", "choice"
_ASSESSMENT_RE = re.compile(
    r"\b(coding (test|challenge|assessment)|take[- ]home|skills? (test|assessment)|"
    r"leetcode|hackerrank|codility|write (a|the) (code|function|program)|implement (a|the)|"
    r"solve (this|the following)|complete the (test|assessment|challenge))\b", re.I)
_STORY_RE = re.compile(
    r"\b(tell (us|me) about a time|describe a (situation|time|challenge)|give an example|"
    r"a time when|how did you handle|walk (us|me) through a)\b", re.I)


@dataclass
class SheetItem:
    n: int
    question: str
    qtype: str
    status: str                       # answered | needs_input | story_needed | assessment_skipped
    answer: str = ""
    gap: str = ""
    build_questions: list[str] = field(default_factory=list)


@dataclass
class AnswerSheet:
    items: list[SheetItem] = field(default_factory=list)
    source: str = ""

    @property
    def needs_attention(self) -> list[SheetItem]:
        return [i for i in self.items if i.status != "answered"]

    def render(self) -> str:
        lines = [f"📝 Answer sheet ({len(self.items)} question(s)) — review before anything is sent."]
        for it in self.items:
            lines.append(f"\n{it.n}. {it.question}")
            if it.status == "answered":
                lines.append(f"   → {it.answer}")
            elif it.status == "needs_input":
                lines.append(f"   ⚠️ NEEDS YOUR INPUT — {it.gap}")
            elif it.status == "story_needed":
                lines.append("   ⚠️ No matching story on file. Quick questions to build one:")
                lines.extend(f"      • {q}" for q in it.build_questions)
            elif it.status == "assessment_skipped":
                lines.append("   ⛔ Skills test/assessment — I won't solve this for you. "
                             "Run /prep for a practice pack (Phase 6).")
        pending = len(self.needs_attention)
        lines.append(f"\n{'✅ All answered.' if not pending else f'⚠️ {pending} item(s) need you before this is ready.'}"
                     "\nNothing is submitted until you approve.")
        return "\n".join(lines)


class FormAssistSkill(BaseSkill):
    name = "form_assist"

    def __init__(
        self,
        engine: PersonaEngine | None = None,
        llm: LLMClient | None = None,
        mailer: ApplicationMailer | None = None,
        fetch: Callable[[str], str | None] | None = None,
    ) -> None:
        self._engine = engine
        self._llm = llm
        self._mailer = mailer
        self._fetch = fetch

    @property
    def engine(self) -> PersonaEngine:
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def mailer(self) -> ApplicationMailer:
        if self._mailer is None:
            self._mailer = ApplicationMailer()
        return self._mailer

    def contract(self) -> SkillContract:
        """Reads `tone` and `jobs` — it writes answers in Calvin's voice on job applications.

        The two skill-specific invariants are the ones no instruction may argue with: an
        unknown answer is flagged rather than guessed (§0 P5), and a skills test is never
        solved for him no matter how a rule is phrased.
        """
        return SkillContract(reads_categories=["tone", "jobs"],
                             hard_invariants=["never_guesses_an_unknown_answer",
                                              "never_solves_an_assessment"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "answer": self.answer,
            "parse": self.parse_only,
            "build_story": self.build_story,
            "submit": self.submit,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- fetch
    def _get_content(self, content: str, url: str) -> str:
        if content.strip():
            return content
        if url:
            if self._fetch is not None:
                return self._fetch(url) or ""
            try:
                from skills.job_hunter.fetcher import Fetcher

                resp = Fetcher().get(url, accept="text/html")
                return resp.text if resp is not None else ""
            except Exception:  # noqa: BLE001
                log.exception("form fetch failed for %s", url)
                return ""
        return ""

    # ------------------------------------------------------------- parse
    def parse_questions(self, content: str) -> list[dict[str, str]]:
        """Extract questions with a type. LLM first, heuristic fallback so it works offline."""
        try:
            data = self.llm.chat_json(
                "classify",
                [{"role": "system", "content":
                    "Extract every question/field a candidate must answer from this form or screener. "
                    "Classify each type: 'short' (one line), 'long' (paragraph), 'story' (behavioral "
                    "'tell us about a time'), 'assessment' (coding/skills test to solve), 'choice' "
                    "(multiple choice). Return JSON."},
                 {"role": "user", "content": content[:6000]}],
                schema_hint='{"questions": [{"question": string, "type": string}]}',
                temperature=0.0, max_tokens=800,
            )
            qs = [{"question": q.get("question", "").strip(),
                   "type": self._normalize_type(q.get("question", ""), q.get("type", ""))}
                  for q in data.get("questions", []) if q.get("question")]
            if qs:
                return qs
        except LLMError:
            log.warning("parse_questions LLM failed — using heuristic split")
        return self._heuristic_questions(content)

    def _heuristic_questions(self, content: str) -> list[dict[str, str]]:
        lines = [ln.strip(" -•\t") for ln in content.splitlines()]
        out = []
        for ln in lines:
            if len(ln) < 6:
                continue
            if ln.endswith("?") or re.match(r"^\d+[.)]", ln) or ln.lower().startswith(
                    ("describe", "tell", "explain", "what", "why", "how", "list", "provide")):
                q = re.sub(r"^\d+[.)]\s*", "", ln)
                out.append({"question": q, "type": self._normalize_type(q, "")})
        return out

    @staticmethod
    def _normalize_type(question: str, given: str) -> str:
        if _ASSESSMENT_RE.search(question):
            return ASSESSMENT
        if _STORY_RE.search(question):
            return STORY
        g = (given or "").lower()
        if g in (SHORT, LONG, STORY, ASSESSMENT, CHOICE):
            return g
        return SHORT

    # ------------------------------------------------------------- answer
    def answer(self, content: str = "", url: str = "", **_: Any) -> CommandResult:
        """Build a review-ready answer sheet. Never submits (approval required, §0)."""
        raw = self._get_content(content, url)
        if not raw.strip():
            return CommandResult(text="Give me the form questions (paste text or a URL).", ok=False)

        questions = self.parse_questions(raw)
        if not questions:
            return CommandResult(text="I couldn't find any questions in that.", ok=False)

        sheet = AnswerSheet(source=url or "pasted")
        for i, q in enumerate(questions, start=1):
            sheet.items.append(self._answer_one(i, q["question"], q["type"]))

        return CommandResult(
            text=sheet.render(),
            data={"total": len(sheet.items), "pending": len(sheet.needs_attention),
                  "items": [it.__dict__ for it in sheet.items]},
        )

    def _answer_one(self, n: int, question: str, qtype: str) -> SheetItem:
        if qtype == ASSESSMENT:
            return SheetItem(n, question, qtype, status="assessment_skipped")

        if qtype == STORY:
            story = self.engine.best_story(question)
            if story:
                return SheetItem(n, question, qtype, status="answered", answer=story["value"])
            return SheetItem(n, question, qtype, status="story_needed",
                             build_questions=[
                                 "What was the situation/context?",
                                 "What did YOU specifically do?",
                                 "What was the result (numbers if any)?"])

        ans = self.engine.answer(question)
        if ans.needs_input:
            return SheetItem(n, question, qtype, status="needs_input", gap=ans.gap)
        return SheetItem(n, question, qtype, status="answered", answer=ans.text)

    def parse_only(self, content: str = "", url: str = "", **_: Any) -> CommandResult:
        raw = self._get_content(content, url)
        qs = self.parse_questions(raw)
        body = "\n".join(f"{i+1}. [{q['type']}] {q['question']}" for i, q in enumerate(qs))
        return CommandResult(text=body or "No questions found.", data={"questions": qs})

    # ------------------------------------------------------------- story building
    def build_story(self, key: str = "", situation: str = "", task: str = "",
                    action: str = "", result: str = "", **_: Any) -> CommandResult:
        """Assemble and save a STAR story from Calvin's inputs, for reuse in behavioral Qs."""
        if not key or not (situation or action):
            return CommandResult(text="Need at least a key and the situation/action to build a story.",
                                 ok=False)
        star = (f"Situation: {situation}\nTask: {task}\nAction: {action}\nResult: {result}").strip()
        self.engine.add_story(key, star, verified=True)
        return CommandResult(text=f"Saved story '{key}' — I'll reuse it for similar behavioral questions.",
                             data={"key": key})

    # ------------------------------------------------------------- submit (approval-gated)
    def submit(self, to: str = "", subject: str = "", answers: str = "",
               approved: bool = False, **_: Any) -> CommandResult:
        """Email approved answers to a form's submit address. Requires approved=True (§0)."""
        if not approved:
            return CommandResult(
                text="Not sent — submission requires explicit approval. Review the sheet, then approve.",
                ok=False, data={"blocked": "approval_required"})
        if not to or not answers.strip():
            return CommandResult(text="Need a submit address and the approved answer text.", ok=False)
        try:
            self.mailer.send_application(to=to, subject=subject or "Application form", body=answers)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Send failed: {exc}", ok=False)
        return CommandResult(text=f"Submitted to {to}.", data={"to": to})


SKILL = FormAssistSkill()
