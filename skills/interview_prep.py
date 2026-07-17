"""Interview prep packs & mock interviews (Phase 6).

Auto-triggered on interview_invite detection (job-hunter watcher) or via /prep <company>:
researches the company, then generates 15 likely questions with suggested answers in
Calvin's voice (grounded in the persona KB), 3 questions for him to ask, and a logistics
checklist — delivered as a Telegram message plus a clean charcoal ReportLab PDF.
/mock <company> runs an interactive one-question-at-a-time mock with brief candid feedback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.pdf import build_pdf
from core.persona_store import verified_facts_text
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.interview_prep")

_PACK_SCHEMA = ('{"company_summary": string, '
                '"questions": [{"q": string, "a": string}], '
                '"ask_them": [string], "checklist": [string]}')
_MOCK_KEY = "interview_prep.mock"


class InterviewPrepSkill(BaseSkill):
    name = "interview_prep"

    def __init__(self, llm: LLMClient | None = None, memory: Memory | None = None,
                 research: Any | None = None,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._llm = llm
        self._mem = memory
        self._research = research
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

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
    def research(self):
        if self._research is None:
            from skills.research import ResearchSkill

            self._research = ResearchSkill()
        return self._research

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "prep": self.prep,
            "mock": self.mock,
            "mock_answer": self.mock_answer,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- prep pack
    def generate_pack(self, company: str, role: str = "") -> dict[str, Any]:
        """Research + generate the structured prep pack (no delivery). Returns the pack dict."""
        research = self.research.research(f"{company} company what they do product recent news "
                                          f"interview process for {role or 'engineering'} roles")
        facts = verified_facts_text(self.mem) or "(persona not seeded — answers stay generic)"
        sys = (
            "You are an interview coach preparing Calvin. Using the company research and Calvin's "
            "verified facts, produce a prep pack. Suggested answers must be in Calvin's voice, first "
            "person, and ONLY use his verified facts — never invent experience. Return JSON with "
            "EXACTLY 15 questions."
        )
        user = (f"COMPANY: {company}\nROLE: {role or 'general engineering'}\n\n"
                f"RESEARCH:\n{research.cited_text()[:2500]}\n\n"
                f"CALVIN'S VERIFIED FACTS:\n{facts}")
        try:
            pack = self.llm.chat_json("write",
                                      [{"role": "system", "content": sys},
                                       {"role": "user", "content": user}],
                                      schema_hint=_PACK_SCHEMA, temperature=0.4, max_tokens=2200)
        except LLMError:
            log.exception("prep pack generation failed")
            pack = {"company_summary": research.answer, "questions": [], "ask_them": [], "checklist": []}
        pack["company"] = company
        pack["role"] = role
        pack["sources"] = [s.__dict__ for s in research.sources]
        return pack

    def prep(self, company: str = "", role: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Full prep flow: generate pack, write PDF, push a Telegram summary."""
        if not company.strip():
            return CommandResult(text="Which company? Usage: /prep <company>.", ok=False)
        pack = self.generate_pack(company.strip(), role)
        pdf_path = self._write_pdf(pack)
        summary = self._telegram_summary(pack, pdf_path)
        if notify:
            self._notify(summary)
        return CommandResult(text=summary,
                             data={"company": company, "pdf": str(pdf_path),
                                   "questions": len(pack.get("questions", []))})

    def _write_pdf(self, pack: dict[str, Any]) -> Path:
        company = pack["company"]
        stamp = time.strftime("%Y%m%d")
        safe = "".join(c for c in company if c.isalnum() or c in " -_").strip().replace(" ", "_")
        out = get_settings().data_dir / "prep" / f"{safe}_{stamp}.pdf"

        qa_paras = []
        for i, qa in enumerate(pack.get("questions", []), start=1):
            qa_paras.append(f"Q{i}. {qa.get('q', '')}")
            qa_paras.append(f"- {qa.get('a', '')}")
        sections = [
            (f"About {company}", [pack.get("company_summary", "")]),
            ("Likely questions & suggested answers", qa_paras or ["(none generated)"]),
            ("Questions for Calvin to ask them", [f"- {x}" for x in pack.get("ask_them", [])] or ["(none)"]),
            ("Logistics checklist", [f"- {x}" for x in pack.get("checklist", [])] or ["(none)"]),
        ]
        subtitle = f"Interview prep pack · {pack.get('role') or 'engineering role'} · {time.strftime('%d %b %Y')}"
        return build_pdf(out, f"{company} — Interview Prep", sections, subtitle=subtitle)

    def _telegram_summary(self, pack: dict[str, Any], pdf_path: Path) -> str:
        qs = pack.get("questions", [])
        lines = [f"🎯 Interview prep: {pack['company']}",
                 f"{pack.get('company_summary', '')[:400]}",
                 f"\n{len(qs)} likely questions prepared. Top 3:"]
        for qa in qs[:3]:
            lines.append(f"• {qa.get('q', '')}")
        lines.append(f"\nAsk THEM: " + "; ".join(pack.get("ask_them", [])[:3]))
        lines.append(f"\n📄 Full pack (with your suggested answers): {pdf_path.name}")
        lines.append("Type /mock " + pack["company"] + " to rehearse.")
        return "\n".join(lines)

    # ------------------------------------------------------------- mock interview
    def mock(self, company: str = "", **_: Any) -> CommandResult:
        """Start an interactive mock: generate questions, ask the first one."""
        if not company.strip():
            return CommandResult(text="Which company should I mock-interview you for?", ok=False)
        pack = self.generate_pack(company.strip())
        questions = [qa.get("q", "") for qa in pack.get("questions", []) if qa.get("q")][:8]
        if not questions:
            questions = ["Tell me about yourself.", "Why this role?", "Describe a hard problem you solved."]
        session = {"company": company.strip(), "questions": questions, "idx": 0, "scores": []}
        self.mem.kv_set(_MOCK_KEY, json.dumps(session))
        return CommandResult(
            text=f"Mock interview for {company} — {len(questions)} questions. Answer each; I'll give "
                 f"brief feedback.\n\nQ1. {questions[0]}",
            data={"question": questions[0], "index": 0, "total": len(questions)})

    def mock_answer(self, answer: str = "", **_: Any) -> CommandResult:
        """Grade the current answer with brief candid feedback and advance."""
        raw = self.mem.kv_get(_MOCK_KEY)
        if not raw:
            return CommandResult(text="No mock in progress. Start one with /mock <company>.", ok=False)
        session = json.loads(raw)
        idx = session["idx"]
        questions = session["questions"]
        question = questions[idx]

        feedback = self._grade(session["company"], question, answer)
        session["scores"].append(feedback)
        session["idx"] = idx + 1

        if session["idx"] >= len(questions):
            self.mem.kv_set(_MOCK_KEY, "")
            return CommandResult(
                text=f"{feedback}\n\n✅ That was the last question. Good work — review the prep PDF for "
                     "the areas we flagged.",
                data={"done": True})
        self.mem.kv_set(_MOCK_KEY, json.dumps(session))
        nxt = questions[session["idx"]]
        return CommandResult(text=f"{feedback}\n\nQ{session['idx'] + 1}. {nxt}",
                             data={"question": nxt, "index": session["idx"], "done": False})

    def _grade(self, company: str, question: str, answer: str) -> str:
        if not answer.strip():
            return "Feedback: no answer given — take a beat and try to structure it (STAR for behavioral)."
        try:
            return self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "You are a candid but supportive interview coach. In 2-3 sentences, give feedback on "
                    "this answer: what worked, one concrete improvement. Be honest, not flattering."},
                 {"role": "user", "content": f"Company: {company}\nQ: {question}\nCalvin's answer: {answer}"}],
                max_tokens=180,
            ).strip()
        except LLMError:
            return "Feedback: (coach model unavailable) — make sure you gave a concrete, specific example."


SKILL = InterviewPrepSkill()
