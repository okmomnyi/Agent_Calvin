"""Persona skill (Phase 4).

Exposes the persona engine to the intent router / Telegram / voice: standing instructions
(remember / forget / instructions), answering as Calvin (ask), browsing facts, and the
learning loop (nightly distill of edits, weekly style regeneration). Nothing here ever
invents facts — answer() returns NEEDS_INPUT when something is unknown (§0).
"""

from __future__ import annotations

from typing import Any, Callable

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.notify import send_telegram
from core.persona_store import PersonaEngine, get_engine
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.persona")

_GH_SCHEMA = ('{"facts": [{"category": string, "key": string, "value": string, '
              '"evidence": string}]}')
# What GitHub can honestly speak to. Deliberately excludes rates, availability and
# work_authorization: no repository tells you Calvin's day-rate or notice period, and a model
# asked for them would invent them.
_GH_CATEGORIES = ["skills", "tools", "work_history", "preferences", "languages"]


class PersonaSkill(BaseSkill):
    name = "persona"

    def __init__(self, engine: PersonaEngine | None = None,
                 notify: Callable[[str], bool] | None = None,
                 llm: LLMClient | None = None,
                 gh_evidence: Callable[[str], str] | None = None) -> None:
        self._engine = engine
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram
        self._llm = llm
        self._gh = gh_evidence

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def gh_evidence(self) -> Callable[[str], str]:
        if self._gh is None:
            from core.github_profile import evidence

            self._gh = evidence
        return self._gh

    @property
    def engine(self) -> PersonaEngine:
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    # ------------------------------------------------------------- github import (Phase 25)
    def import_github(self, user: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Derive CANDIDATE persona facts from Calvin's own public repos.

        His 45 repos are better evidence of what he builds than any interview answer -- the CV
        says "Cloud & Infrastructure", the repos say TypeScript, Node backends, an election
        system. But a machine reading a README does not get to decide what is true about him:
        every fact lands UNVERIFIED and waits for /facts (§0 Principle 5). A README saying
        "experimenting with Kubernetes" must never reach an employer as "uses Kubernetes".

        GitHub only. LinkedIn forbids automated access and blocks it, and it is Calvin's
        account that gets restricted -- the same reasoning that keeps this project off
        Facebook Marketplace. Paste LinkedIn text and `remember` it instead.
        """
        user = (user or "").strip() or self._default_gh_user()
        if not user:
            return CommandResult(text="Which GitHub username? e.g. `persona github okmomnyi`.",
                                 ok=False)
        try:
            evidence = self.gh_evidence(user)
        except Exception as exc:  # noqa: BLE001 - network/ratelimit/404 all land here
            return CommandResult(text=f"Couldn't read GitHub for '{user}': {exc}", ok=False)

        try:
            data = self.llm.chat_json(
                # 'write', not 'research'. This is structured extraction from text we already
                # have, not reasoning that needs a 120B model -- and the research route
                # (nemotron-3-super-120b) answered this prompt with "We need to output" followed
                # by a wall of <unk> tokens. The write route has been consistently sane and fast.
                "write",
                [{"role": "system", "content":
                    "From this GitHub evidence, extract factual, defensible statements about the "
                    "developer for use in job applications. Rules: state ONLY what the evidence "
                    "supports; never infer seniority, years of experience, or employment; prefer "
                    "concrete counts and named projects over adjectives; if a README merely says "
                    "'learning' or 'experimenting with' X, do NOT claim X as a skill. "
                    f"Allowed categories: {', '.join(_GH_CATEGORIES)}. "
                    "Give each fact a short `evidence` string citing the repo(s) it came from."},
                 {"role": "user", "content": evidence[:5000]}],
                schema_hint=_GH_SCHEMA, temperature=0.1, max_tokens=700)
        except LLMError as exc:
            return CommandResult(text=f"Couldn't summarize GitHub right now: {exc}", ok=False)

        added = 0
        for f in data.get("facts", []):
            cat, key, val = f.get("category"), f.get("key"), f.get("value")
            if not (cat and key and val) or cat not in _GH_CATEGORIES:
                continue
            ev = f.get("evidence", "")
            self.engine.add_fact(
                cat, key, f"{val}" + (f" (from GitHub: {ev})" if ev else ""),
                # 0.6 and unverified: honest about provenance. A machine read a README.
                confidence=0.6, source=f"github:{user}", verified=False)
            added += 1

        text = (f"📦 Read {user}'s public repos → {added} candidate fact(s), all UNVERIFIED.\n"
                f"Nothing will be used in a cover letter until you confirm it: `/facts` to "
                f"review, or `persona verify <category> <key>`.\n"
                f"(GitHub only — LinkedIn blocks automated access; paste text and I'll "
                f"`remember` it.)")
        if notify and added:
            self._notify(text)
        return CommandResult(text=text, data={"user": user, "candidates": added})

    @staticmethod
    def _default_gh_user() -> str:
        import os

        from core.config import get_settings

        return (os.getenv("GITHUB_USER", "").strip()
                or str(get_settings().get("github", "user", default="")).strip())

    def import_github_detailed(self, user: str = "", notify: bool = True,
                               **_: Any) -> CommandResult:
        """Rich, deterministic seed: languages, deployed projects, and every collaboration.

        Unlike import_github (which asks an LLM to summarise), this reads structured facts
        straight from the API -- so it can't be broken by a NIM timeout, and every fact is
        verbatim from GitHub. Collaborations (UMS, Project47, ZameenEye-AI, ZKSentinel, ...)
        come from config.github.collaborations: the contributor graph is public, so Calvin's
        commit count and teammates are real. Still candidates until he confirms (§0 P5).
        """
        from core.config import get_settings
        from core.github_profile import derive_facts

        user = (user or "").strip() or self._default_gh_user()
        if not user:
            return CommandResult(text="Which GitHub username?", ok=False)
        collab = list(get_settings().get("github", "collaborations", default=[]) or [])
        try:
            facts = derive_facts(user, collab)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Couldn't read GitHub for '{user}': {exc}", ok=False)

        added = 0
        for f in facts:
            cat, key, val = f.get("category"), f.get("key"), f.get("value")
            if not (cat and key and val) or cat not in _GH_CATEGORIES:
                continue
            ev = f.get("evidence", "")
            self.engine.add_fact(cat, key, f"{val}" + (f" (from GitHub: {ev})" if ev else ""),
                                 confidence=0.7, source=f"github:{user}", verified=False)
            added += 1
        text = (f"📦 Deep GitHub read of {user} → {added} detailed candidate fact(s), "
                f"UNVERIFIED.\nIncludes your languages, deployed projects, and every "
                f"collaboration (with commit counts + teammates).\nConfirm with `/facts` or "
                f"`persona verify <category> <key>`; nothing reaches a cover letter unconfirmed.")
        if notify and added:
            self._notify(text)
        return CommandResult(text=text, data={"user": user, "candidates": added})

    def verify(self, category: str = "", key: str = "", accept: bool = True,
               **_: Any) -> CommandResult:
        """Promote a candidate fact to verified (or reject it). Calvin's call, always."""
        if not category or not key:
            return CommandResult(text="Usage: persona verify <category> <key> [--reject]", ok=False)
        self.engine.verify_fact(category, key, bool(accept))
        verb = "confirmed" if accept else "rejected"
        return CommandResult(text=f"{'✅' if accept else '🚫'} {verb}: {category}.{key}")

    def candidates(self, **_: Any) -> CommandResult:
        """Everything waiting on Calvin's confirmation."""
        rows = [f for f in self.engine.get_facts() if not f.get("verified")]
        if not rows:
            return CommandResult(text="No candidate facts awaiting confirmation.",
                                 data={"count": 0})
        lines = [f"🕵️ {len(rows)} candidate fact(s) — none are used until you confirm:"]
        for f in rows[:30]:
            lines.append(f"  • [{f['category']}] {f['key']}: {str(f['value'])[:90]}")
        lines.append("\nConfirm with `persona verify <category> <key>`.")
        return CommandResult(text="\n".join(lines), data={"count": len(rows)})

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "github": self.import_github,
            "github_detailed": self.import_github_detailed,
            "import_github": self.import_github,
            "verify": self.verify,
            "candidates": self.candidates,
            "remember": self.remember,
            "forget": self.forget,
            "instructions": self.list_instructions,
            "answer": self.answer,
            "ask": self.answer,
            "facts": self.facts,
            "learn": self.learn,
            "regenerate_style": self.regenerate_style,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="persona.distill", func=self.learn, trigger="cron",
                         kwargs={"hour": 2, "minute": 30}),
            ScheduledJob(id="persona.style", func=self.regenerate_style, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 3}),
        ]

    # ------------------------------------------------------------- standing instructions
    def remember(self, instruction: str = "", **_: Any) -> CommandResult:
        if not instruction.strip():
            return CommandResult(text="Remember what? Give me an instruction.", ok=False)
        self.engine.remember(instruction.strip())
        return CommandResult(text=f"Got it — I'll remember: “{instruction.strip()}”.",
                             data={"instructions": self.engine.instructions()})

    def forget(self, instruction: str = "", **_: Any) -> CommandResult:
        if not instruction.strip():
            return CommandResult(text="Forget what? Give me the instruction to drop.", ok=False)
        self.engine.forget(instruction.strip())
        return CommandResult(text=f"Dropped: “{instruction.strip()}”.",
                             data={"instructions": self.engine.instructions()})

    def list_instructions(self, **_: Any) -> CommandResult:
        items = self.engine.instructions()
        if not items:
            return CommandResult(text="No standing instructions yet. Add one with /remember.")
        body = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
        return CommandResult(text=f"Standing instructions:\n{body}", data={"instructions": items})

    # ------------------------------------------------------------- answer as Calvin
    def answer(self, question: str = "", context: str = "", **_: Any) -> CommandResult:
        if not question.strip():
            return CommandResult(text="Ask me a question to answer as you.", ok=False)
        ans = self.engine.answer(question.strip(), context=context)
        if ans.needs_input:
            return CommandResult(
                text=f"I don't have a verified answer for that. {ans.gap}",
                data={"needs_input": True, "gap": ans.gap}, ok=False,
            )
        return CommandResult(text=ans.text, data={"facts_used": ans.facts_used})

    # ------------------------------------------------------------- facts browsing
    def facts(self, category: str = "", **_: Any) -> CommandResult:
        rows = self.engine.get_facts(category or None)
        if not rows:
            return CommandResult(text="No persona facts yet — run `manage.py persona-init`.")
        lines = []
        for r in rows:
            mark = "✓" if r["verified"] else "?"
            lines.append(f"{mark} [{r['category']}] {r['key']}: {r['value']}")
        unverified = [r for r in rows if not r["verified"]]
        head = f"{len(rows)} fact(s)" + (f", {len(unverified)} awaiting confirmation" if unverified else "")
        return CommandResult(text=f"{head}:\n" + "\n".join(lines), data={"count": len(rows)})

    # ------------------------------------------------------------- learning loop
    def learn(self, notify: bool = True, **_: Any) -> CommandResult:
        """Distill candidate facts from Calvin's edits; queue them for confirmation."""
        candidates = self.engine.distill_edits()
        if not candidates:
            return CommandResult(text="No new candidate facts from recent edits.", data={"candidates": 0})
        body = "\n".join(f"- [{c['category']}] {c['key']}: {c['value']}" for c in candidates)
        msg = (f"🧠 I learned {len(candidates)} possible new fact(s) from your edits "
               f"(unverified — confirm with /facts):\n{body}")
        if notify:
            self._notify(msg)
        return CommandResult(text=msg, data={"candidates": len(candidates)})

    def regenerate_style(self, **_: Any) -> CommandResult:
        profile = self.engine.regenerate_style()
        return CommandResult(text=profile or "Not enough approved samples to build a style profile yet.",
                             data={"has_style": bool(profile)})


SKILL = PersonaSkill()
