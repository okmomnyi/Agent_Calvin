"""Offline guardrails for Phase 35's model -> validation -> dispatch boundary."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.approvals import ASK
from core.intent import Intent, IntentRouter
from core.orchestrator import (Orchestrator, Plan, PlanStep, PlanValidationError,
                               should_plan)
from core.skill import BaseSkill, CommandResult
from kernel.registry import SkillRegistry


class _Memory:
    def __init__(self):
        self.plans = {}
        self.steps = {}

    def save_plan(self, plan):
        self.plans[plan["id"]] = deepcopy(plan)

    def save_plan_step(self, plan_id, step):
        self.steps[(plan_id, step["id"])] = deepcopy(step)

    def get_plan(self, plan_id):
        plan = self.plans.get(plan_id)
        if not plan:
            return None
        out = deepcopy(plan)
        out["steps"] = [deepcopy(v) for (pid, _), v in self.steps.items() if pid == plan_id]
        return out

    def current_plan(self, session_id="calvin"):
        active = [p for p in self.plans.values()
                  if p["session_id"] == session_id and p["status"] in {
                      "planning", "awaiting_approval", "executing", "paused"}]
        return self.get_plan(active[-1]["id"]) if active else None


class _Approvals:
    def __init__(self, decision=ASK):
        self.decision = decision

    def decision_for(self, _key):
        return self.decision


class _LLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_json(self, task, messages, schema_hint, **kwargs):
        self.calls.append((task, messages, schema_hint, kwargs))
        return deepcopy(self.payload)


class _Registry:
    def __init__(self, actions, tiers=None, handlers=None):
        self.actions = list(actions)
        self.tiers = tiers or {}
        self.handlers = handlers or {}
        self.dispatched = []

    def manifest(self):
        return [{"skill": s, "action": a, "doc": "", "args": {}}
                for s, a in self.actions]

    def action_tier(self, skill, action):
        return self.tiers.get((skill, action), "trivial")

    def dispatch_intent(self, intent):
        self.dispatched.append(intent)
        handler = self.handlers.get((intent.skill, intent.action))
        return handler(intent.args) if handler else CommandResult(text=f"ran {intent.action}")


def _step(step_id, action, *, deps=None, produces=None, args=None, **extra):
    raw = {"id": step_id, "skill": "work", "action": action, "args": args or {},
           "depends_on": deps or [], "produces": produces, "rationale": action}
    raw.update(extra)
    return raw


def _orchestrator(payload, registry, memory=None, approvals=None):
    return Orchestrator(registry=registry, llm=_LLM(payload), memory=memory or _Memory(),
                        approvals=approvals or _Approvals())


def test_manifest_lists_real_actions_and_honours_plan_exclude():
    class Example(BaseSkill):
        name = "example"
        plan_exclude = ("internal",)

        def commands(self):
            return {"visible": self.visible, "internal": self.visible}

        def visible(self):
            """Do a visible thing."""
            return CommandResult("ok")

    reg = SkillRegistry(planning_enabled=False)
    reg._skills["example"] = Example()  # isolate discovery/database from this unit test
    assert [(m["skill"], m["action"]) for m in reg.manifest()] == [("example", "visible")]
    assert reg.manifest()[0]["doc"] == "Do a visible thing."


def test_unknown_action_and_its_dependents_are_dropped():
    registry = _Registry([("work", "finish")])
    orch = _orchestrator({"steps": []}, registry)
    plan = Plan("p", "goal", [
        PlanStep("s1", "invented", "magic"),
        PlanStep("s2", "work", "finish", depends_on=["s1"]),
    ])
    validated = orch.validate_plan(plan)
    assert validated.steps == []
    assert any("unknown action" in gap for gap in validated.gaps)
    assert any("depends on a dropped step" in gap for gap in validated.gaps)


def test_dangling_dependency_and_cycle_fail_the_whole_plan():
    registry = _Registry([("work", "one"), ("work", "two")])
    orch = _orchestrator({"steps": []}, registry)
    with pytest.raises(PlanValidationError, match="dangling"):
        orch.validate_plan(Plan("p", "goal", [PlanStep("s1", "work", "one", depends_on=["x"])]))
    with pytest.raises(PlanValidationError, match="cycle"):
        orch.validate_plan(Plan("p", "goal", [
            PlanStep("s1", "work", "one", depends_on=["s2"]),
            PlanStep("s2", "work", "two", depends_on=["s1"]),
        ]))


def test_model_claimed_trivial_tier_is_discarded_and_high_always_asks():
    registry = _Registry([("work", "send")], tiers={("work", "send"): "high"})
    payload = {"steps": [_step("s1", "send", tier="trivial")]}
    orch = _orchestrator(payload, registry)
    result = orch.run("send this in my name")
    assert result.ok
    assert result.data["awaiting_approval"] is True
    assert result.data["steps"][0]["tier"] == "high"
    assert registry.dispatched == []


def test_topological_execution_resolves_prior_output_from_scratchpad():
    seen = []

    def collect(_args):
        return CommandResult("collected", data={"items": [1, 2, 3]})

    def finish(args):
        seen.append(args["items"])
        return CommandResult("finished", data={"count": len(args["items"])})

    registry = _Registry(
        [("work", "collect"), ("work", "finish")],
        handlers={("work", "collect"): collect, ("work", "finish"): finish},
    )
    payload = {"steps": [
        _step("s2", "finish", deps=["s1"], args={"items": "$s1.items"}),
        _step("s1", "collect", produces="items"),
    ]}
    result = _orchestrator(payload, registry).run("collect, and then finish")
    assert result.ok
    assert [i.action for i in registry.dispatched] == ["collect", "finish"]
    assert seen == [[1, 2, 3]]


def test_unresolvable_output_reference_drops_step_and_dependent():
    registry = _Registry([("work", "collect"), ("work", "finish")])
    orch = _orchestrator({"steps": []}, registry)
    plan = Plan("p", "goal", [
        PlanStep("s1", "work", "collect", produces="items"),
        PlanStep("s2", "work", "finish", args={"x": "$s1.other"}, depends_on=["s1"]),
    ])
    validated = orch.validate_plan(plan)
    assert [s.id for s in validated.steps] == ["s1"]
    assert any("is not produced" in gap for gap in validated.gaps)


def test_failure_pauses_and_fresh_orchestrator_resumes_without_rerunning_done_steps():
    attempts = {"first": 0, "second": 0}

    def first(_args):
        attempts["first"] += 1
        return CommandResult("first done")

    def second(_args):
        attempts["second"] += 1
        if attempts["second"] == 1:
            return CommandResult("temporary failure", ok=False)
        return CommandResult("second done")

    memory = _Memory()
    registry = _Registry(
        [("work", "first"), ("work", "second")],
        handlers={("work", "first"): first, ("work", "second"): second},
    )
    payload = {"steps": [
        _step("s1", "first"), _step("s2", "second", deps=["s1"]),
    ]}
    initial = _orchestrator(payload, registry, memory=memory)
    paused = initial.run("first, and then second")
    assert not paused.ok and paused.data["paused"]
    assert attempts == {"first": 1, "second": 1}

    fresh = _orchestrator(payload, registry, memory=memory)
    resumed = fresh.resume(paused.data["plan_id"])
    assert resumed.ok
    assert attempts == {"first": 1, "second": 2}


def test_empty_plan_is_honest_and_dispatches_nothing():
    registry = _Registry([("work", "one")])
    result = _orchestrator(
        {"steps": [], "reason": "No capability can book flights."}, registry,
    ).run("book me a flight")
    assert not result.ok
    assert "No capability can book flights" in result.text
    assert registry.dispatched == []


def test_plan_and_step_state_round_trip_through_postgres(mem):
    mem.save_plan({"id": "db-plan", "goal": "test persistence", "status": "planning",
                   "channel": "cli", "session_id": "calvin", "reason": "", "gaps": []})
    mem.save_plan_step("db-plan", {
        "id": "s1", "skill": "work", "action": "one", "args": {"x": 1},
        "depends_on": [], "produces": "answer", "rationale": "test", "tier": "trivial",
        "status": "done", "output": {"answer": 42}, "error": None, "attempts": 1,
    })
    mem.save_plan({"id": "db-plan", "goal": "test persistence", "status": "paused",
                   "channel": "cli", "session_id": "calvin", "reason": "", "gaps": ["gap"]})
    loaded = mem.get_plan("db-plan")
    assert loaded["status"] == "paused"
    assert loaded["gaps"] == ["gap"]
    assert loaded["steps"][0]["output"] == {"answer": 42}
    assert mem.current_plan()["id"] == "db-plan"


def test_goal_heuristic_is_conservative_and_mode_phrases_are_not_goals():
    assert should_plan("find my deadlines, and then build a study schedule")
    assert should_plan("get me ready for next week")
    assert not should_plan("tutor me on recursion")
    assert not should_plan("create a playlist for coding")


def test_keyword_mode_never_reaches_planner():
    class Router(IntentRouter):
        def route(self, text, *, use_llm=True):
            return Intent("tutor", "mode", "start", {"topic": "recursion"},
                          confidence=0.9, via="keyword")

    class Mode(BaseSkill):
        name = "mode"

        def commands(self):
            return {"start": lambda **_: CommandResult("tutoring")}

    class NeverPlanner:
        def handle_reply(self, _text):
            raise AssertionError("planner state should not be consulted for a mode command")

        def run(self, *_args, **_kwargs):
            raise AssertionError("mode keyword reached planner")

    reg = SkillRegistry(router=Router(), orchestrator=NeverPlanner(), planning_enabled=True)
    reg._skills["mode"] = Mode()
    intent, result = reg.handle_command("tutor me on recursion")
    assert intent.name == "tutor"
    assert result.text == "tutoring"
