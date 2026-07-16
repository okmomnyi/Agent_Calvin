"""Persona engine tests: facts-only answering, NEEDS_INPUT on gaps (§0: never invent),
standing instructions, the learning-loop distiller, and the seeding interview flow."""

from __future__ import annotations

from core.llm import LLMClient, LLMError
from core.persona_store import NEEDS_INPUT, PersonaEngine
from core.persona_init import PersonaInterview


class _PersonaLLM(LLMClient):
    """Scriptable LLM: chat_json returns a fixed payload; records calls."""

    def __init__(self, payload=None, raise_error=False):
        self.routes = {"default": "m", "persona": "m", "write": "m"}
        self.defaults = {}
        self._payload = payload
        self._raise = raise_error
        self.json_calls = 0

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        self.json_calls += 1
        if self._raise:
            raise LLMError("down")
        return self._payload

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "style: concise, direct"


def _engine(mem, payload=None, raise_error=False):
    return PersonaEngine(llm=_PersonaLLM(payload, raise_error), memory=mem)


# ------------------------------------------------------------------ answer()
def test_answer_needs_input_when_no_facts(mem):
    eng = _engine(mem, payload={"answer": "x", "needs_input": False, "gap": ""})
    ans = eng.answer("what is your AWS certification?")
    assert ans.needs_input is True
    assert ans.text == NEEDS_INPUT
    # short-circuits before calling the LLM (no facts to ground an answer)
    assert eng.llm.json_calls == 0


def test_answer_uses_verified_facts(mem):
    eng = _engine(mem, payload={"answer": "I use Docker, PM2, Caddy and Nginx daily.",
                                "needs_input": False, "gap": ""})
    eng.add_fact("tools", "devops_tools", "Docker, PM2, Caddy, Nginx, Cloudflare", verified=True)
    ans = eng.answer("what devops tools do you use?")
    assert ans.needs_input is False
    assert "Docker" in ans.text
    assert ans.facts_used >= 1


def test_answer_respects_model_needs_input_signal(mem):
    # A fact is retrieved, but the model reports the question isn't fully covered -> NEEDS_INPUT.
    eng = _engine(mem, payload={"answer": "", "needs_input": True, "gap": "exact typing speed unknown"})
    eng.add_fact("skills", "transcription_tools", "Express Scribe, oTranscribe", verified=True)
    ans = eng.answer("what is your transcription typing speed in wpm?")
    assert ans.needs_input is True
    assert "typing speed" in ans.gap


def test_answer_llm_failure_is_safe(mem):
    eng = _engine(mem, raise_error=True)
    eng.add_fact("skills", "cloud", "AWS basics", verified=True)
    ans = eng.answer("tell me about your cloud skills")
    assert ans.needs_input is True  # fails safe, never fabricates


def test_unverified_facts_excluded_from_answers(mem):
    eng = _engine(mem, payload={"answer": "no", "needs_input": True, "gap": "unknown"})
    eng.add_fact("skills", "kubernetes", "expert", verified=False)  # candidate, not confirmed
    assert eng.retrieve("kubernetes experience") == []


# ------------------------------------------------------------------ standing instructions
def test_standing_instructions_add_list_forget(mem):
    eng = _engine(mem)
    eng.remember("prioritize cloud/DevOps over transcription this month")
    eng.remember("don't message me before 8am")
    assert len(eng.instructions()) == 2
    eng.forget("don't message me before 8am")
    assert eng.instructions() == ["prioritize cloud/DevOps over transcription this month"]


def test_relevant_instructions_filter(mem):
    eng = _engine(mem)
    eng.remember("always tailor my CV for cloud roles")
    eng.remember("don't message me before 8am")
    rel = eng.relevant_instructions(["cv", "cloud"])
    assert rel == ["always tailor my CV for cloud roles"]


# ------------------------------------------------------------------ learning loop
def test_distill_edits_creates_unverified_candidates(mem):
    eng = _engine(mem, payload={"facts": [
        {"category": "preferences", "key": "signoff", "value": "signs off as 'Cheers, Calvin'"},
    ]})
    mem.log_edit("cover_letter", "job X", draft="Best regards, Calvin", edited="Cheers, Calvin")
    candidates = eng.distill_edits()
    assert len(candidates) == 1
    # stored as UNVERIFIED — needs Calvin's confirmation, not auto-trusted
    facts = eng.get_facts("preferences")
    assert facts[0]["verified"] == 0
    # qa_log row marked distilled so it isn't reprocessed
    assert eng.distill_edits() == []


def test_verify_fact_confirms_and_rejects(mem):
    eng = _engine(mem)
    eng.add_fact("skills", "terraform", "used on 2 projects", verified=False)
    eng.verify_fact("skills", "terraform", accept=True)
    assert eng.get_facts("skills")[0]["verified"] == 1
    eng.verify_fact("skills", "terraform", accept=False)
    assert eng.get_facts("skills")[0]["verified"] == 0


# ------------------------------------------------------------------ interview
def test_persona_interview_stores_answers(mem):
    eng = _engine(mem)
    scripted = iter([
        "",                       # name -> accept default (Calvin)
        "Mombasa, Kenya",         # location
        "skip",                   # university -> skip
        "",                       # year -> default
        "skip", "AWS Cloud Practitioner in progress", "Docker, PM2, Caddy",
        "skip", "skip", "2 weeks", "skip", "skip", "github.com/calvin", "English, Swahili",
    ])
    said: list[str] = []
    interview = PersonaInterview(engine=eng, ask_fn=lambda p: next(scripted), say_fn=said.append)
    stored = interview.run()
    assert stored >= 5
    facts = {(f["category"], f["key"]): f["value"] for f in eng.get_facts(verified_only=True)}
    assert facts[("bio", "name")] == "Calvin"          # accepted default
    assert facts[("bio", "location")] == "Mombasa, Kenya"
    assert ("education", "university") not in facts     # skipped
    assert facts[("skills", "cloud_certs")] == "AWS Cloud Practitioner in progress"
