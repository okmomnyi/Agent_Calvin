"""Code tutor mode (Phase 12).

Five modes over Calvin's coursework and his cloud/DevOps + light security tracks:
  * explain  — concept explanations with C++/Python examples at his level (raw pointers /
               manual linked lists in C++, not just STL);
  * review   — line-level feedback on HIS code via the strongest coder model, phrased as
               teaching with a "what to try next" hint — NOT a rewrite unless he asks after
               attempting;
  * drill    — generated practice problems on a difficulty ladder; checks his solution and
               turns topics he fails into flashcard candidates (Phase 11);
  * socratic — guiding questions and small hints only; "just tell me" is the escape hatch;
  * mock lab — a timed problem set graded at the end with a rubric.

§0 study rule: the tutor teaches, it does not ghost-write assignments, and it never solves
live CTF challenges for him — those get explained, not answered.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.code_tutor")

MODES = ("explain", "review", "drill", "socratic", "mocklab")
_SESSION_KEY = "code_tutor.session"

# Refuse to solve a live/graded CTF or assignment outright — offer to teach instead.
_LIVE_CTF_RE = re.compile(
    r"\b(give me the flag|what('?s| is) the flag|solve (this|the) (ctf|challenge|flag)|"
    r"crack this|exploit this for me|the answer to (my|this) (assignment|homework|lab))\b", re.I)


class CodeTutorSkill(BaseSkill):
    name = "code_tutor"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._llm = llm
        self._now = clock

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

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "start": self.start, "explain": self.explain, "review": self.review,
            "drill": self.drill, "drill_check": self.drill_check, "socratic": self.socratic,
            "mocklab": self.mocklab, "mocklab_submit": self.mocklab_submit,
            "continue": self.continue_session, "end": self.end,
        }

    def contract(self) -> SkillContract:
        """Reads study rules only — a tone rule can never reach the grading logic. And no
        instruction can ever make it hand over a finished assignment or a live CTF answer."""
        return SkillContract(reads_categories=["study"],
                             hard_invariants=["never_emit_finished_assignment",
                                              "never_solve_live_ctf"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    def _level(self) -> str:
        return ("Calvin is a Year-3 CS student comfortable with C++ (raw pointers, manual data "
                "structures), Python, and growing cloud/DevOps skills. Match examples to that level.")

    def _session(self) -> dict[str, Any] | None:
        raw = self.mem.kv_get(_SESSION_KEY)
        return json.loads(raw) if raw else None

    def _save(self, s: dict[str, Any]) -> None:
        self.mem.kv_set(_SESSION_KEY, json.dumps(s))

    # ------------------------------------------------------------- start (parses mode)
    def start(self, topic: str = "", **_: Any) -> CommandResult:
        """Enter tutor mode. 'drill linked lists' / 'socratic recursion' / 'mock lab graphs'."""
        text = topic.strip()
        mode = "explain"
        low = text.lower()
        for m in MODES:
            key = "mock lab" if m == "mocklab" else m
            if low.startswith(key):
                mode = m
                text = text[len(key):].strip(" ,:-")
                break
        if mode == "explain":
            return self.explain(topic=text)
        if mode == "drill":
            return self.drill(topic=text)
        if mode == "socratic":
            return self.socratic(question=text)
        if mode == "mocklab":
            return self.mocklab(topic=text)
        if mode == "review":
            return CommandResult(text="Paste the code you want reviewed and I'll give feedback.",
                                 data={"mode": "review"})
        return self.explain(topic=text)

    def end(self, **_: Any) -> CommandResult:
        self.mem.kv_set(_SESSION_KEY, "")
        return CommandResult(text="Tutor session ended.")

    # ------------------------------------------------------------- explain
    def explain(self, topic: str = "", **_: Any) -> CommandResult:
        if not topic.strip():
            return CommandResult(text="What topic should I explain?", ok=False)
        try:
            text = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    f"You are a patient CS tutor. {self._level()} Explain the topic clearly with a short "
                    "worked example in C++ AND/OR Python as appropriate. Prefer showing mechanics (e.g. "
                    "manual linked-list nodes in C++) over hiding them behind the STL. End with one check "
                    "question. Do not write full assignment solutions."},
                 {"role": "user", "content": f"Explain: {topic}"}], max_tokens=800)
        except LLMError:
            return CommandResult(text="Couldn't reach the tutor model right now.", ok=False)
        self._save({"mode": "explain", "topic": topic})
        return CommandResult(text=text, data={"mode": "explain", "topic": topic})

    # ------------------------------------------------------------- review
    def review(self, code: str = "", unit: str = "", rewrite: bool = False, **_: Any) -> CommandResult:
        """Line-level teaching feedback on Calvin's code (bugs/memory/complexity/style)."""
        if not code.strip():
            return CommandResult(text="Paste the code you want reviewed.", ok=False)
        rule = ("Give a full corrected version at the end." if rewrite else
                "Do NOT provide a full rewritten solution — give a 'what to try next' hint so he fixes it "
                "himself. Only rewrite if he explicitly asks after attempting.")
        try:
            text = self.llm.chat(
                "code_review",
                [{"role": "system", "content":
                    f"You are a code reviewer teaching a student. {self._level()} Give concise line-level "
                    f"feedback: bugs, memory/ownership issues, complexity, and style — as teaching, not "
                    f"scolding. {rule}"},
                 {"role": "user", "content": f"Review this code:\n\n{code[:6000]}"}], max_tokens=900)
        except LLMError:
            return CommandResult(text="Couldn't reach the review model right now.", ok=False)
        return CommandResult(text=text, data={"mode": "review", "rewrote": rewrite})

    # ------------------------------------------------------------- drill
    def drill(self, topic: str = "", difficulty: int = 1, **_: Any) -> CommandResult:
        if self._is_live_ctf(topic):
            return self._ctf_refusal()
        if not topic.strip():
            return CommandResult(text="What topic should I drill you on?", ok=False)
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    f"Generate ONE practice problem (not a full assignment) on the topic at difficulty "
                    f"{difficulty}/5 for a CS student. {self._level()} Return JSON."},
                 {"role": "user", "content": f"Topic: {topic}"}],
                schema_hint='{"problem": string, "hint": string}', temperature=0.5, max_tokens=500)
        except LLMError:
            return CommandResult(text="Couldn't generate a problem right now.", ok=False)
        self._save({"mode": "drill", "topic": topic, "difficulty": difficulty,
                    "problem": data.get("problem", ""), "hint": data.get("hint", "")})
        return CommandResult(text=f"🧩 Drill ({topic}, level {difficulty}):\n\n{data.get('problem','')}\n\n"
                                  "Reply with your solution.",
                             data={"mode": "drill", "problem": data.get("problem", "")})

    def drill_check(self, answer: str = "", **_: Any) -> CommandResult:
        s = self._session()
        if not s or s.get("mode") != "drill":
            return CommandResult(text="No active drill. Start with 'tutor drill <topic>'.", ok=False)
        try:
            data = self.llm.chat_json(
                "code_review",
                [{"role": "system", "content":
                    "Assess the student's solution to the practice problem. Be encouraging but honest. "
                    "Return JSON: correct(bool), feedback(teaching, with a next hint if wrong), and — if "
                    "they got it wrong — a flashcard (front,back) capturing the concept they missed."},
                 {"role": "user", "content":
                    f"PROBLEM: {s['problem']}\nSTUDENT SOLUTION:\n{answer[:4000]}"}],
                schema_hint='{"correct": bool, "feedback": string, "flashcard": {"front": string, "back": string}}',
                temperature=0.2, max_tokens=600)
        except LLMError:
            return CommandResult(text="Couldn't assess that right now.", ok=False)

        correct = bool(data.get("correct"))
        if not correct:
            card = data.get("flashcard") or {}
            if card.get("front") and card.get("back"):
                self.mem.add_flashcard(card["front"].strip(), card["back"].strip(),
                                       unit=s["topic"], source="tutor:drill", status="candidate")
        next_diff = min(5, s["difficulty"] + 1) if correct else s["difficulty"]
        nxt = self.drill(topic=s["topic"], difficulty=next_diff)
        tag = "✅ Correct!" if correct else "❌ Not quite — added the concept to your flashcards to review."
        return CommandResult(text=f"{tag}\n{data.get('feedback','')}\n\n{nxt.text}",
                             data={"correct": correct, "leveled_up": correct})

    # ------------------------------------------------------------- socratic
    def socratic(self, question: str = "", **_: Any) -> CommandResult:
        s = self._session() or {}
        history = s.get("history", []) if s.get("mode") == "socratic" else []
        if question.strip().lower() in ("just tell me", "just tell me the answer", "tell me"):
            return self._socratic_reveal(history)
        try:
            reply = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "You are a Socratic tutor. NEVER give the direct answer. Respond ONLY with 1-2 guiding "
                    "questions and at most a small hint, leading the student to work it out. If they say "
                    "'just tell me', that is handled elsewhere."},
                 {"role": "user", "content": question}], max_tokens=300)
        except LLMError:
            return CommandResult(text="Couldn't reach the tutor right now.", ok=False)
        history = (history + [question])[-6:]
        self._save({"mode": "socratic", "history": history, "last": question})
        return CommandResult(text=reply + "\n\n(Say “just tell me” if you want the answer.)",
                             data={"mode": "socratic"})

    def _socratic_reveal(self, history: list[str]) -> CommandResult:
        topic = history[0] if history else "the question"
        try:
            answer = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "Give the direct answer now, plus a 2-line explanation of the underlying idea. Concise."},
                 {"role": "user", "content": f"Question/topic: {topic}"}], max_tokens=350)
        except LLMError:
            answer = "Couldn't reach the model."
        self.mem.kv_set(_SESSION_KEY, "")
        return CommandResult(text=answer, data={"mode": "socratic", "revealed": True})

    # ------------------------------------------------------------- mock lab
    def mocklab(self, topic: str = "", minutes: int = 60, count: int = 3, **_: Any) -> CommandResult:
        if self._is_live_ctf(topic):
            return self._ctf_refusal()
        if not topic.strip():
            return CommandResult(text="What topic for the mock lab?", ok=False)
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    f"Create a {count}-question timed practice lab on the topic (practice, NOT a real "
                    f"assignment). {self._level()} Return JSON."},
                 {"role": "user", "content": f"Topic: {topic}"}],
                schema_hint='{"questions": [string]}', temperature=0.5, max_tokens=800)
        except LLMError:
            return CommandResult(text="Couldn't build the lab right now.", ok=False)
        questions = data.get("questions", [])[:count]
        self._save({"mode": "mocklab", "topic": topic, "questions": questions,
                    "started": self._now(), "minutes": minutes})
        body = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        return CommandResult(
            text=f"⏱ Mock lab — {topic} ({minutes} min, {len(questions)} questions):\n\n{body}\n\n"
                 "Reply with all your answers when done (I'll grade with a rubric).",
            data={"mode": "mocklab", "questions": questions})

    def mocklab_submit(self, answers: str = "", **_: Any) -> CommandResult:
        s = self._session()
        if not s or s.get("mode") != "mocklab":
            return CommandResult(text="No active mock lab.", ok=False)
        elapsed = (self._now() - s["started"]) / 60
        try:
            data = self.llm.chat_json(
                "code_review",
                [{"role": "system", "content":
                    "Grade this mock lab against a rubric. Return JSON: score(0-100), per_question "
                    "(list of {feedback}), overall(string), weak_topics(list of string)."},
                 {"role": "user", "content":
                    f"QUESTIONS:\n{json.dumps(s['questions'])}\n\nANSWERS:\n{answers[:6000]}"}],
                schema_hint='{"score": int, "per_question": [{"feedback": string}], '
                            '"overall": string, "weak_topics": [string]}',
                temperature=0.2, max_tokens=1000)
        except LLMError:
            return CommandResult(text="Couldn't grade the lab right now.", ok=False)
        # weak topics -> flashcard candidates for Phase 11
        for wt in data.get("weak_topics", []):
            if wt:
                self.mem.add_flashcard(f"Review: {wt}", f"Weak area from {s['topic']} mock lab — revise {wt}.",
                                       unit=s["topic"], source="tutor:mocklab", status="candidate")
        self.mem.kv_set(_SESSION_KEY, "")
        pq = "\n".join(f"  Q{i+1}: {q.get('feedback','')}" for i, q in enumerate(data.get("per_question", [])))
        return CommandResult(
            text=f"📝 Mock lab graded — {data.get('score','?')}/100 (took {elapsed:.0f} min)\n{pq}\n\n"
                 f"{data.get('overall','')}\nWeak areas added to your flashcards: "
                 f"{', '.join(data.get('weak_topics', [])) or 'none'}.",
            data={"score": data.get("score"), "weak_topics": data.get("weak_topics", [])})

    # ------------------------------------------------------------- session continuation
    def continue_session(self, text: str = "", **_: Any) -> CommandResult:
        """Route free text to the active tutor mode (drill solution / socratic answer / lab submit)."""
        s = self._session()
        if not s:
            return CommandResult(text="No tutor session active. Start with 'tutor <mode> <topic>'.", ok=False)
        mode = s.get("mode")
        if mode == "drill":
            return self.drill_check(answer=text)
        if mode == "socratic":
            return self.socratic(question=text)
        if mode == "mocklab":
            return self.mocklab_submit(answers=text)
        if mode == "review":
            return self.review(code=text)
        return self.explain(topic=text)

    # ------------------------------------------------------------- guardrails
    @staticmethod
    def _is_live_ctf(text: str) -> bool:
        return bool(_LIVE_CTF_RE.search(text or ""))

    def _ctf_refusal(self) -> CommandResult:
        return CommandResult(
            text=("I won't solve a live CTF or graded assignment for you — that's the §0 rule and it keeps "
                  "you out of trouble. I can explain the underlying concept, walk through a *similar* "
                  "practice example, or review your own attempt. Which would help?"),
            ok=False, data={"refused": "live_ctf_or_assignment"})


SKILL = CodeTutorSkill()
