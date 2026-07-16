"""Code tutor tests: mode routing, review-not-rewrite, drill→flashcard, socratic refuse +
'just tell me', mock-lab grading→flashcards, the live-CTF/ghostwriter guard, and continuation."""

from __future__ import annotations

from core.llm import LLMClient
from skills.code_tutor import CodeTutorSkill


class _TutorLLM(LLMClient):
    def __init__(self, chat_text="tutor reply", json_payload=None):
        self.routes = {"default": "m", "write": "m", "code_review": "m", "classify": "m"}
        self.defaults = {}
        self._chat = chat_text
        self._json = json_payload or {}
        self.last_task = None

    def chat(self, task, messages, **kw):  # type: ignore[override]
        self.last_task = task
        return self._chat

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        self.last_task = task
        return self._json


def _skill(mem, llm):
    return CodeTutorSkill(memory=mem, llm=llm, clock=lambda: 1_000.0)


# ------------------------------------------------------------------ start / explain
def test_start_parses_mode(mem):
    llm = _TutorLLM(json_payload={"problem": "reverse a linked list", "hint": "use three pointers"})
    skill = _skill(mem, llm)
    res = skill.start(topic="drill linked lists")
    assert res.data["mode"] == "drill"
    assert "reverse a linked list" in res.text
    # session persisted for continuation
    assert skill._session()["mode"] == "drill"


def test_explain_uses_write_model(mem):
    llm = _TutorLLM(chat_text="A linked list is a chain of nodes...")
    res = _skill(mem, llm).explain(topic="linked lists")
    assert "chain of nodes" in res.text
    assert llm.last_task == "write"


# ------------------------------------------------------------------ review
def test_review_uses_code_review_model_and_no_rewrite_by_default(mem):
    llm = _TutorLLM(chat_text="Line 3 leaks memory — what should free it? Try adding a destructor.")
    res = _skill(mem, llm).review(code="int* p = new int[5];")
    assert llm.last_task == "code_review"
    assert res.data["rewrote"] is False


# ------------------------------------------------------------------ drill -> flashcard
def test_drill_wrong_answer_creates_flashcard_candidate(mem):
    # first drill() returns a problem
    gen = _TutorLLM(json_payload={"problem": "invert a BST", "hint": "swap children recursively"})
    skill = _skill(mem, gen)
    skill.drill(topic="trees")
    # now the check returns 'incorrect' with a concept flashcard
    skill._llm = _TutorLLM(json_payload={
        "correct": False, "feedback": "Close, but you didn't recurse.",
        "flashcard": {"front": "How to invert a BST?", "back": "Swap left/right recursively"}})
    # re-seed the problem generator for the follow-up drill() call inside drill_check
    res = skill.drill_check(answer="return root")
    assert res.data["correct"] is False
    assert mem.count_flashcards(unit="trees", status="candidate") == 1


def test_drill_correct_levels_up(mem):
    skill = _skill(mem, _TutorLLM(json_payload={"problem": "p1", "hint": "h"}))
    skill.drill(topic="trees", difficulty=2)
    skill._llm = _TutorLLM(json_payload={"correct": True, "feedback": "Nice.", "flashcard": {}})
    res = skill.drill_check(answer="correct solution")
    assert res.data["correct"] is True and res.data["leveled_up"] is True


# ------------------------------------------------------------------ socratic
def test_socratic_refuses_direct_answer(mem):
    llm = _TutorLLM(chat_text="What happens to the base case when n=0?")
    res = _skill(mem, llm).socratic(question="how does recursion work?")
    assert "just tell me" in res.text.lower()
    assert res.data["mode"] == "socratic"


def test_socratic_just_tell_me_reveals(mem):
    skill = _skill(mem, _TutorLLM(chat_text="guiding q"))
    skill.socratic(question="what is a hash collision?")
    skill._llm = _TutorLLM(chat_text="A collision is when two keys hash to the same bucket. ...")
    res = skill.socratic(question="just tell me")
    assert res.data.get("revealed") is True
    assert "collision" in res.text
    assert not skill._session()  # session cleared after reveal


# ------------------------------------------------------------------ mock lab
def test_mocklab_grades_and_adds_weak_topics(mem):
    skill = _skill(mem, _TutorLLM(json_payload={"questions": ["Q1", "Q2", "Q3"]}))
    skill.mocklab(topic="graphs", minutes=45)
    skill._llm = _TutorLLM(json_payload={
        "score": 60, "per_question": [{"feedback": "ok"}, {"feedback": "wrong"}, {"feedback": "ok"}],
        "overall": "Revise traversal.", "weak_topics": ["BFS vs DFS", "Dijkstra"]})
    res = skill.mocklab_submit(answers="my answers")
    assert res.data["score"] == 60
    assert mem.count_flashcards(unit="graphs", status="candidate") == 2  # two weak topics -> cards


# ------------------------------------------------------------------ guardrails
def test_live_ctf_is_refused(mem):
    skill = _skill(mem, _TutorLLM())
    res = skill.drill(topic="give me the flag for this CTF challenge")
    assert res.ok is False
    assert res.data.get("refused") == "live_ctf_or_assignment"


def test_mocklab_refuses_assignment_solving(mem):
    skill = _skill(mem, _TutorLLM())
    res = skill.mocklab(topic="the answer to my assignment on sorting")
    assert res.ok is False


# ------------------------------------------------------------------ continuation
def test_continue_routes_by_mode(mem):
    skill = _skill(mem, _TutorLLM(json_payload={"problem": "p", "hint": "h"}))
    skill.drill(topic="stacks")
    skill._llm = _TutorLLM(json_payload={"correct": True, "feedback": "yes", "flashcard": {}})
    res = skill.continue_session(text="my solution")
    assert res.data.get("correct") is True   # routed to drill_check
