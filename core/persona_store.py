"""Persona engine — AgentOS gets to know Calvin and answers as him (Phase 4).

Backed by the persona_facts + standing_instructions + cv_facts + qa_log tables. Core
guarantees (§0): answer() responds ONLY from verified facts and returns NEEDS_INPUT
(never a guess) when a required fact is missing — one invented qualification is a hard
failure. Also holds standing instructions (behavior rules Calvin gives the agent) and
the continuous learning loop that distills style/facts from Calvin's edits for his
confirmation. "Calvin's voice" here means writing tone only — never audio (§0 Principle 9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.persona")

FACT_CATEGORIES = [
    "bio", "education", "work_history", "skills", "tools", "languages", "availability",
    "rates", "preferences", "writing_style", "stories", "academics",
    "flips",   # measured flip performance from the Phase 18 margin ledger (never guessed)
    "music",   # measured listening taste from Spotify (Phase 22)
]

NEEDS_INPUT = "NEEDS_INPUT"
_STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "does", "what", "whats", "your", "you", "my",
    "me", "i", "of", "to", "for", "in", "on", "and", "or", "with", "have", "has", "can",
    "how", "when", "where", "which", "any", "about", "tell", "give",
}


@dataclass
class Answer:
    """Result of persona.answer(). needs_input=True means a fact is missing — never guessed."""

    text: str
    needs_input: bool = False
    gap: str = ""
    facts_used: int = 0


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOPWORDS and len(t) > 1]


class PersonaEngine:
    """Read/write persona facts, answer as Calvin from verified facts, hold standing rules."""

    def __init__(self, llm: LLMClient | None = None, memory: Memory | None = None) -> None:
        self._llm = llm
        self._mem = memory

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

    # ------------------------------------------------------------- facts
    def add_fact(self, category: str, key: str, value: str, *, confidence: float = 0.9,
                 source: str = "interview", verified: bool = True) -> None:
        if category not in FACT_CATEGORIES:
            log.warning("Unknown persona category '%s' (allowed: %s)", category, FACT_CATEGORIES)
        self.mem.upsert_fact(category, key, value, confidence=confidence, source=source, verified=verified)

    def get_facts(self, category: str | None = None, *, verified_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM persona_facts"
        clauses, params = [], []
        if category:
            clauses.append("category=%s")
            params.append(category)
        if verified_only:
            clauses.append("verified=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY category, key"
        return [dict(r) for r in self.mem.execute(sql, params).fetchall()]

    def verify_fact(self, category: str, key: str, accept: bool) -> None:
        """Confirm (verified=1) or reject (deactivate via confidence 0) a candidate fact."""
        if accept:
            with self.mem.tx():
                self.mem.conn.execute(
                    "UPDATE persona_facts SET verified=1, confidence=GREATEST(confidence,0.9) "
                    "WHERE category=%s AND key=%s", (category, key))
        else:
            # never delete data — mark unverified with zero confidence
            with self.mem.tx():
                self.mem.conn.execute(
                    "UPDATE persona_facts SET verified=0, confidence=0 WHERE category=%s AND key=%s",
                    (category, key))

    # ------------------------------------------------------------- retrieval
    def retrieve(self, question: str, k: int = 8) -> list[dict[str, Any]]:
        """Keyword-overlap retrieval over verified facts (category+key+value)."""
        q = set(_tokens(question))
        if not q:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for fact in self.get_facts(verified_only=True):
            hay = set(_tokens(f"{fact['category']} {fact['key']} {fact['value']}"))
            overlap = len(q & hay)
            if overlap:
                scored.append((overlap, fact))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:k]]

    # ------------------------------------------------------------- answer (facts-only)
    def answer(self, question: str, context: str = "") -> Answer:
        """Answer as Calvin using ONLY verified facts. Returns NEEDS_INPUT on any gap."""
        facts = self.retrieve(question)
        if not facts:
            return Answer(text=NEEDS_INPUT, needs_input=True,
                          gap=f"No verified fact covers: {question!r}. Ask Calvin.")

        facts_block = "\n".join(f"- [{f['category']}] {f['key']}: {f['value']}" for f in facts)
        sys = (
            "You answer strictly AS Calvin, in first person, concise and direct. "
            "Use ONLY the verified facts provided. If they do not fully answer the question, "
            "you MUST set needs_input=true and put the specific missing item in 'gap' — NEVER "
            "invent or infer a fact. Return JSON only."
        )
        user = (f"VERIFIED FACTS:\n{facts_block}\n\n"
                f"{'CONTEXT: ' + context + chr(10) if context else ''}"
                f"QUESTION: {question}")
        try:
            data = self.llm.chat_json(
                "persona",
                [{"role": "system", "content": sys}, {"role": "user", "content": user}],
                schema_hint='{"answer": string, "needs_input": bool, "gap": string}',
                temperature=0.2, max_tokens=400,
            )
        except LLMError:
            log.warning("persona.answer LLM failed — returning NEEDS_INPUT (safe default)")
            return Answer(text=NEEDS_INPUT, needs_input=True,
                          gap="Couldn't reach the model to answer safely.")

        if data.get("needs_input"):
            return Answer(text=NEEDS_INPUT, needs_input=True,
                          gap=str(data.get("gap") or question), facts_used=len(facts))
        return Answer(text=str(data.get("answer", "")).strip(), facts_used=len(facts))

    # ------------------------------------------------------------- story bank (STAR)
    def add_story(self, key: str, star_text: str, *, verified: bool = True) -> None:
        """Save a STAR-format experience anecdote for reuse in behavioral questions."""
        self.add_fact("stories", key, star_text, confidence=1.0, source="stories", verified=verified)

    def stories(self) -> list[dict[str, Any]]:
        return self.get_facts("stories", verified_only=True)

    def best_story(self, question: str) -> dict[str, Any] | None:
        """Return the best keyword-matching verified story for a behavioral question, or None."""
        q = set(_tokens(question))
        if not q:
            return None
        best, best_score = None, 0
        for f in self.stories():
            hay = set(_tokens(f"{f['key']} {f['value']}"))
            overlap = len(q & hay)
            if overlap > best_score:
                best, best_score = f, overlap
        return best if best_score > 0 else None

    # ------------------------------------------------------------- standing instructions
    def remember(self, instruction: str, category: str = "general",
                 source: str = "calvin") -> None:
        self.mem.add_instruction(instruction, category=category, source=source)

    def forget(self, instruction: str) -> None:
        self.mem.deactivate_instruction(instruction)

    def instructions(self) -> list[str]:
        return [r["instruction"] for r in self.mem.list_instructions(active_only=True)]

    def instructions_for_skill(self, skill_name: str) -> list[str]:
        """Only the rules a skill's CONTRACT declares it reads (Phase 20 boundary check).

        Anything outside that scope is ignored, not applied — a tone rule can never reach
        into, say, Code Tutor's grading logic.
        """
        rows = self.mem.execute(
            "SELECT reads_categories FROM skill_contracts WHERE skill_name=%s",
            (skill_name,)).fetchone()
        if not rows:
            return []                       # no registered contract -> reads nothing
        cats = [c for c in rows["reads_categories"].split(",") if c]
        return [r["instruction"] for r in self.mem.list_instructions(categories=cats)]

    def relevant_instructions(self, keywords: list[str] | None = None) -> list[str]:
        """Active instructions matching any keyword (or all active if no keywords given).

        Every skill consults this before acting — this is what keeps the agent morphing
        around Calvin over time rather than staying static after the initial seed.
        """
        active = self.instructions()
        if not keywords:
            return active
        kw = {k.lower() for k in keywords}
        return [i for i in active if any(k in i.lower() for k in kw)]

    # ------------------------------------------------------------- style profile
    def style_profile(self) -> str:
        return self.mem.kv_get("persona.style", "") or ""

    def regenerate_style(self) -> str:
        """Rebuild the writing-style profile from approved outputs (weekly). Best-effort."""
        rows = self.mem.execute(
            "SELECT edited FROM qa_log WHERE edited IS NOT NULL ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        samples = [r["edited"] for r in rows if r["edited"]]
        if not samples:
            return self.style_profile()
        try:
            profile = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "From these writing samples, extract a short style profile for reuse: tone, "
                    "sentence length, phrases used, phrases/words avoided. 5 bullet points max."},
                 {"role": "user", "content": "\n---\n".join(samples[:20])}],
                max_tokens=300,
            )
        except LLMError:
            return self.style_profile()
        self.mem.kv_set("persona.style", profile)
        return profile

    # ------------------------------------------------------------- learning loop
    def distill_edits(self) -> list[dict[str, Any]]:
        """Nightly: turn undistilled (draft, edited) pairs into UNVERIFIED candidate facts.

        Candidates are stored verified=0 for Calvin to confirm (via Telegram, Phase 8).
        Returns the candidate facts extracted this run.
        """
        rows = self.mem.execute(
            "SELECT id, kind, context, draft, edited FROM qa_log WHERE distilled=0 AND edited IS NOT NULL"
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = self.llm.chat_json(
                    "persona",
                    [{"role": "system", "content":
                        "Compare a draft AgentOS wrote with Calvin's edited version. Extract any NEW "
                        "durable facts about Calvin or style rules his edit reveals. Only extract what "
                        "the edit clearly evidences — do not speculate. Return JSON."},
                     {"role": "user", "content": f"DRAFT:\n{row['draft']}\n\nEDITED:\n{row['edited']}"}],
                    schema_hint='{"facts": [{"category": string, "key": string, "value": string}]}',
                    temperature=0.1, max_tokens=400,
                )
                for f in data.get("facts", []):
                    cat = f.get("category", "preferences")
                    key = f.get("key", "")
                    val = f.get("value", "")
                    if key and val:
                        self.add_fact(cat, key, val, confidence=0.4, source="learning", verified=False)
                        candidates.append({"category": cat, "key": key, "value": val})
            except LLMError:
                log.warning("distill_edits: LLM failed on qa_log %s", row["id"])
                continue
            with self.mem.tx():
                self.mem.conn.execute("UPDATE qa_log SET distilled=1 WHERE id=%s", (row["id"],))
        return candidates


# --------------------------------------------------------------- module helpers (used by other skills)
def verified_facts_text(memory: Memory | None = None) -> str:
    """Compact bullet list of VERIFIED persona + CV facts, or '' if none seeded."""
    mem = memory or get_memory()
    lines: list[str] = []
    for r in mem.execute(
        "SELECT category, key, value FROM persona_facts WHERE verified=1 ORDER BY category, key"
    ).fetchall():
        lines.append(f"- [{r['category']}] {r['key']}: {r['value']}")
    for r in mem.execute("SELECT section, key, value FROM cv_facts ORDER BY section, key").fetchall():
        lines.append(f"- [cv:{r['section']}] {r['key']}: {r['value']}")
    return "\n".join(lines)


def is_seeded(memory: Memory | None = None) -> bool:
    mem = memory or get_memory()
    row = mem.execute("SELECT COUNT(*) c FROM persona_facts WHERE verified=1").fetchone()
    return bool(row and row["c"] > 0)


_default_engine: PersonaEngine | None = None


def get_engine() -> PersonaEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = PersonaEngine()
    return _default_engine
