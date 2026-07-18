"""Interactive persona seeding (`manage.py persona-init`).

Walks Calvin through confirming a handful of known basics and answering gap questions
(transcription tooling, cloud/DevOps certs & tools, rates, availability, work auth,
equipment, links), storing each confirmed answer as a VERIFIED persona fact. The Q&A
flow is a pure, injectable class (ask_fn/say_fn) so it is testable without a real TTY;
CLI just wires it to input()/print(). If Phase 15 has parsed a master CV into cv_facts,
those are shown first as pre-filled candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.persona_store import PersonaEngine, get_engine

log = get_logger("core.persona_init")


@dataclass
class Question:
    category: str
    key: str
    prompt: str
    default: str = ""  # a known/suggested value Calvin can accept with Enter


def _seed_and_gap_questions() -> list[Question]:
    name = get_settings().my_name
    return [
        # Known basics — confirm or correct (defaults pre-filled from the project brief).
        Question("bio", "name", "Your name?", name),
        Question("bio", "location", "Where are you based?", "Nairobi, Kenya (volunteering in Mombasa)"),
        Question("education", "university", "University & course?",
                 "Meru University of Science and Technology — BSc Computer Science"),
        Question("education", "year", "Year of study?", "Year 3"),
        # Gap questions (from the build spec).
        Question("skills", "transcription_tools", "Transcription tools/typing speed (if any)?"),
        Question("skills", "cloud_certs", "Cloud/DevOps certs or coursework in progress "
                 "(AWS/GCP/Azure/Docker/Kubernetes/Terraform)?"),
        Question("tools", "devops_tools", "Tools you actually use "
                 "(Docker/PM2/Caddy/Nginx/Cloudflare/n8n/Kali)?"),
        Question("skills", "security", "Security background (CompTIA Network+/Security+, CTF)?"),
        Question("rates", "freelance_rate", "Freelance day-rate or project-rate by category?"),
        Question("availability", "notice_period", "Notice period / availability to start?"),
        Question("availability", "work_authorization", "Work authorization for remote international roles?"),
        Question("availability", "equipment", "Equipment & internet reliability?"),
        Question("preferences", "portfolio_links", "GitHub / portfolio links?"),
        Question("languages", "languages", "Languages & fluency (e.g. English, Swahili)?"),
    ]


class PersonaInterview:
    """Runs the seeding interview. Inject ask_fn/say_fn for tests; CLI uses input/print."""

    def __init__(
        self,
        engine: PersonaEngine | None = None,
        ask_fn: Callable[[str], str] = input,
        say_fn: Callable[[str], None] = print,
    ) -> None:
        self.engine = engine or get_engine()
        self.ask = ask_fn
        self.say = say_fn

    def run(self) -> int:
        """Ask each question, store confirmed answers as verified facts. Returns count stored."""
        self.say("— AgentOS persona setup — press Enter to accept a suggested value, "
                 "type a new answer to change it, or type 'skip' to leave blank.\n")
        self._show_cv_facts()
        stored = 0
        for q in _seed_and_gap_questions():
            suffix = f" [{q.default}]" if q.default else ""
            raw = self.ask(f"{q.prompt}{suffix} ").strip()
            if raw.lower() == "skip":
                continue
            value = raw or q.default
            if not value:
                continue
            self.engine.add_fact(q.category, q.key, value, confidence=1.0,
                                 source="persona-init", verified=True)
            stored += 1
        self.say(f"\nSaved {stored} verified fact(s). Job-hunter cover letters and form answers "
                 "will now be grounded in these.")
        return stored

    def _show_cv_facts(self) -> None:
        rows = self.engine.mem.execute("SELECT section, key, value FROM cv_facts").fetchall()
        if not rows:
            self.say("(No master CV parsed yet — that arrives in Phase 15. Seeding from your answers.)\n")
            return
        self.say("Facts already parsed from your master CV (Phase 15):")
        for r in rows:
            self.say(f"  [{r['section']}] {r['key']}: {r['value']}")
        self.say("")
