"""Form assistant tests: parsing, facts-only answering with NEEDS_INPUT flagging,
story pull/build, assessment auto-skip (never solve), and never-submit-without-approval."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.llm import LLMClient, LLMError
from core.persona_store import PersonaEngine
from skills.form_assist import ASSESSMENT, STORY, FormAssistSkill


class _FormLLM(LLMClient):
    """chat_json returns scripted parsed questions; persona answers are driven separately."""

    def __init__(self, questions=None, raise_parse=False):
        self.routes = {"default": "m", "classify": "m", "persona": "m"}
        self.defaults = {}
        self._questions = questions or []
        self._raise = raise_parse

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        if self._raise:
            raise LLMError("down")
        return {"questions": self._questions}


class _PersonaAnswerLLM(LLMClient):
    """Drives persona.answer(): returns answer or needs_input based on scripted map."""

    def __init__(self, answers):
        self.routes = {"default": "m", "persona": "m"}
        self.defaults = {}
        self._answers = answers  # keyword -> (answer, needs_input, gap)

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        blob = " ".join(m["content"] for m in messages).lower()
        for key, (ans, ni, gap) in self._answers.items():
            if key in blob:
                return {"answer": ans, "needs_input": ni, "gap": gap}
        return {"answer": "", "needs_input": True, "gap": "unknown"}


def _skill(mem, parse_questions, persona_answers):
    engine = PersonaEngine(llm=_PersonaAnswerLLM(persona_answers), memory=mem)
    return FormAssistSkill(engine=engine, llm=_FormLLM(parse_questions), mailer=MagicMock())


# ------------------------------------------------------------------ parsing + types
def test_assessment_question_is_never_solved(mem):
    mem_engine_facts(mem)
    skill = _skill(mem,
                   parse_questions=[{"question": "Write a function to reverse a linked list", "type": "long"}],
                   persona_answers={})
    result = skill.answer(content="Write a function to reverse a linked list")
    item = result.data["items"][0]
    assert item["qtype"] == ASSESSMENT       # detected regardless of the model's 'long' label
    assert item["status"] == "assessment_skipped"
    assert "won't solve" in result.text or "prep" in result.text.lower()


def test_factual_question_answered_from_kb(mem):
    mem_engine_facts(mem)
    skill = _skill(mem,
                   parse_questions=[{"question": "What DevOps tools do you use?", "type": "short"}],
                   persona_answers={"devops": ("Docker, PM2, Caddy, Nginx.", False, "")})
    result = skill.answer(content="What DevOps tools do you use?")
    assert result.data["items"][0]["status"] == "answered"
    assert "Docker" in result.data["items"][0]["answer"]


def test_unknown_fact_is_flagged_not_guessed(mem):
    mem_engine_facts(mem)
    skill = _skill(mem,
                   parse_questions=[{"question": "What is your expected salary in USD?", "type": "short"}],
                   persona_answers={})  # nothing matches -> needs_input
    result = skill.answer(content="What is your expected salary in USD?")
    item = result.data["items"][0]
    assert item["status"] == "needs_input"
    assert result.data["pending"] == 1


def test_behavioral_pulls_story_when_available(mem):
    engine = PersonaEngine(llm=_PersonaAnswerLLM({}), memory=mem)
    engine.add_story("outage", "Situation: prod outage. Action: I rolled back via PM2. Result: 5 min recovery.")
    skill = FormAssistSkill(engine=engine, llm=_FormLLM(
        [{"question": "Tell us about a time you handled a production outage", "type": "story"}]),
        mailer=MagicMock())
    result = skill.answer(content="Tell us about a time you handled a production outage")
    item = result.data["items"][0]
    assert item["qtype"] == STORY
    assert item["status"] == "answered"
    assert "rolled back" in item["answer"]


def test_behavioral_without_story_asks_to_build_one(mem):
    mem_engine_facts(mem)
    skill = _skill(mem,
                   parse_questions=[{"question": "Tell us about a time you led a team", "type": "story"}],
                   persona_answers={})
    result = skill.answer(content="Tell us about a time you led a team")
    item = result.data["items"][0]
    assert item["status"] == "story_needed"
    assert len(item["build_questions"]) == 3


def test_heuristic_fallback_when_parser_llm_fails(mem):
    mem_engine_facts(mem)
    engine = PersonaEngine(llm=_PersonaAnswerLLM({"name": ("I'm Calvin.", False, "")}), memory=mem)
    skill = FormAssistSkill(engine=engine, llm=_FormLLM([], raise_parse=True), mailer=MagicMock())
    content = "1. What is your name?\n2. Describe your ideal role."
    result = skill.answer(content=content)
    assert result.data["total"] == 2   # split heuristically without the LLM


# ------------------------------------------------------------------ approval gate
def test_submit_blocked_without_approval(mem):
    skill = _skill(mem, parse_questions=[], persona_answers={})
    result = skill.submit(to="hr@co.com", answers="answers", approved=False)
    assert result.ok is False
    assert result.data.get("blocked") == "approval_required"
    skill.mailer.send_application.assert_not_called()


def test_submit_sends_only_after_approval(mem):
    skill = _skill(mem, parse_questions=[], persona_answers={})
    result = skill.submit(to="hr@co.com", subject="Form", answers="my answers", approved=True)
    assert result.ok is True
    skill.mailer.send_application.assert_called_once()


def test_build_story_saves_reusable_star(mem):
    engine = PersonaEngine(llm=_PersonaAnswerLLM({}), memory=mem)
    skill = FormAssistSkill(engine=engine, llm=_FormLLM([]), mailer=MagicMock())
    skill.build_story(key="migration", situation="legacy VM", task="move to Docker",
                      action="containerized 6 services", result="zero downtime")
    assert engine.best_story("tell me about a docker migration") is not None


# helper: seed a couple of verified facts so retrieve() has something to match
def mem_engine_facts(mem):
    mem.upsert_fact("tools", "devops_tools", "Docker, PM2, Caddy, Nginx", verified=True)
    mem.upsert_fact("bio", "name", "Calvin", verified=True)
