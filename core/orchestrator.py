"""Goal decomposition, validation, approval preview, and resumable execution.

The planner chooses only *which* registered commands to call and in what order.  It has no
authority path of its own: every surviving step is validated against the live registry and
executed through ``SkillRegistry.dispatch_intent``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from core.approvals import ALWAYS_APPROVE, ALWAYS_DENY, TIER_HIGH, TIER_TRIVIAL, get_store
from core.intent import Intent
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import CommandResult

log = get_logger("core.orchestrator")

PLAN_STATES = {
    "planning", "awaiting_approval", "executing", "paused", "done", "failed", "cancelled",
}
STEP_STATES = {"pending", "running", "done", "failed", "skipped"}
_REF_RE = re.compile(r"^\$(?P<step>[A-Za-z][\w-]*)(?:\.(?P<field>[A-Za-z][\w-]*))?$")
_YES_RE = re.compile(r"^(?:yes|yes all|approve|approve all|ok|okay|do it|go ahead)$", re.I)
_ABORT_RE = re.compile(r"^(?:no|no all|abort|cancel|cancel plan|stop)$", re.I)
_RESUME_RE = re.compile(r"^(?:continue|resume|retry|try again)$", re.I)
_SKIP_RE = re.compile(r"^skip(?:\s+(?P<which>[\w-]+))?$", re.I)


@dataclass
class PlanStep:
    id: str
    skill: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    produces: str | None = None
    rationale: str = ""
    tier: str = "trivial"
    status: str = "pending"
    output: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0


@dataclass
class Plan:
    id: str
    goal: str
    steps: list[PlanStep]
    status: str = "planning"
    channel: str = "cli"
    session_id: str = "calvin"
    reason: str = ""
    gaps: list[str] = field(default_factory=list)
    dropped_steps: list[PlanStep] = field(default_factory=list, repr=False)


class PlanValidationError(ValueError):
    """The proposed graph is structurally unsafe and cannot be partially guessed."""


def should_plan(text: str) -> bool:
    """Conservative, offline compound-goal heuristic.

    Keyword routing runs before this function.  Ambiguous text returns False; a missed goal
    gets ordinary single-command routing, while a mistakenly planned command could trigger a
    surprising series of actions.
    """
    cleaned = " ".join((text or "").strip().lower().split())
    if len(cleaned) < 12:
        return False
    if re.search(r"\b(?:get me ready|help me get ready|sort (?:this|it|everything) out|"
                 r"organize everything|organise everything)\b", cleaned):
        return True
    verbs = re.findall(
        r"\b(?:find|check|build|make|create|draft|write|send|block|schedule|plan|prepare|"
        r"summarize|summarise|research|review|pull|collect|compare|update|clean|sort)\b",
        cleaned,
    )
    explicit_sequence = bool(re.search(r"\b(?:and then|then also|after that|followed by)\b", cleaned))
    compound = bool(re.search(r"(?:;|,)\s*(?:then\s+)?\w+|\b(?:and also|as well as)\b", cleaned))
    return len(verbs) >= 2 and (explicit_sequence or compound)


def is_plan_reply(text: str) -> bool:
    """Whether text is specific enough to justify consulting persisted active-plan state."""
    value = (text or "").strip()
    return bool(_YES_RE.fullmatch(value) or _ABORT_RE.fullmatch(value)
                or _RESUME_RE.fullmatch(value) or _SKIP_RE.fullmatch(value))


class Orchestrator:
    """Injected planner.  LLM, registry, approvals, and memory are replaceable in tests."""

    def __init__(self, *, registry: Any, llm: LLMClient | None = None,
                 approvals: Any | None = None, memory: Memory | None = None,
                 session_id: str = "calvin") -> None:
        self.registry = registry
        self._llm = llm
        self._approvals = approvals
        self._memory = memory
        self.session_id = session_id

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def memory(self) -> Memory:
        if self._memory is None:
            self._memory = get_memory()
        return self._memory

    @property
    def approvals(self):
        if self._approvals is None:
            self._approvals = get_store(self.memory)
        return self._approvals

    # ---------------------------------------------------------- decomposition
    def plan(self, goal: str, *, channel: str = "cli") -> Plan:
        """Ask the reasoning route for strict JSON, then convert only allowed fields."""
        manifest = self.registry.manifest()
        capability_json = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        system = (
            "You are the planner for AgentOS, a personal assistant for Calvin. Turn his goal "
            "into an ordered plan of steps. You may ONLY use actions in the capability list; "
            "never invent a skill or action. Output STRICT JSON {\"steps\":[...]} and nothing "
            "else. Each step has exactly id, skill, action, args, depends_on, produces, and "
            "rationale. Use dependencies for ordering. A step may read prior output with a "
            "reference such as $s1.closing. Keep the plan minimal. If the goal cannot be met, "
            "return {\"steps\":[],\"reason\":\"...\"}. Do not decide approval tiers; extra "
            "authority fields are ignored.\n\nCapability list:\n" + capability_json
        )
        data = self.llm.chat_json(
            "plan",
            [{"role": "system", "content": system},
             {"role": "user", "content": f"Goal: {goal}"}],
            schema_hint=(
                '{"steps":[{"id":str,"skill":str,"action":str,"args":object,'
                '"depends_on":[str],"produces":str|null,"rationale":str}],"reason":str?}'
            ),
            temperature=0.0,
        )
        steps: list[PlanStep] = []
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise PlanValidationError("planner returned 'steps' in a non-list shape")
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise PlanValidationError("planner returned a non-object step")
            # Deliberately enumerate accepted fields.  A model-supplied tier/status/output is
            # discarded at this trust boundary.
            steps.append(PlanStep(
                id=str(raw.get("id", "")).strip(),
                skill=str(raw.get("skill", "")).strip(),
                action=str(raw.get("action", "")).strip(),
                args=dict(raw.get("args") or {}) if isinstance(raw.get("args", {}), dict) else {},
                depends_on=[str(v) for v in (raw.get("depends_on") or [])]
                if isinstance(raw.get("depends_on", []), list) else [],
                produces=(str(raw["produces"]).strip() if raw.get("produces") else None),
                rationale=str(raw.get("rationale", "")).strip()[:500],
            ))
        return Plan(id=uuid.uuid4().hex[:12], goal=goal.strip(), steps=steps,
                    channel=channel, session_id=self.session_id,
                    reason=str(data.get("reason", "")).strip()[:1000])

    # -------------------------------------------------------------- validation
    def validate_plan(self, plan: Plan) -> Plan:
        """Close the model trust boundary against the live registry and approval metadata."""
        ids = [s.id for s in plan.steps]
        if any(not sid for sid in ids) or len(ids) != len(set(ids)):
            raise PlanValidationError("step ids must be non-empty and unique")
        all_ids = set(ids)
        for step in plan.steps:
            dangling = [dep for dep in step.depends_on if dep not in all_ids]
            if dangling:
                raise PlanValidationError(
                    f"step {step.id} has dangling dependencies: {', '.join(dangling)}")
            if step.id in step.depends_on:
                raise PlanValidationError(f"step {step.id} depends on itself")
        self._topological(plan.steps)  # raises on a cycle before any dropping/execution

        allowed = {(m["skill"], m["action"]) for m in self.registry.manifest()}
        dropped = {s.id for s in plan.steps if (s.skill, s.action) not in allowed}
        for step in plan.steps:
            if step.id in dropped:
                plan.gaps.append(f"Dropped {step.id}: unknown action {step.skill}.{step.action}.")
        dropped = self._drop_dependents(plan, dropped, "depends on a dropped step")

        survivors = [s for s in plan.steps if s.id not in dropped]
        by_id = {s.id: s for s in survivors}
        invalid_refs: set[str] = set()
        ancestors = self._ancestors(survivors)
        for step in survivors:
            for ref_step, ref_field in self._references(step.args):
                source = by_id.get(ref_step)
                if source is None or ref_step not in ancestors.get(step.id, set()):
                    invalid_refs.add(step.id)
                    plan.gaps.append(
                        f"Dropped {step.id}: output reference ${ref_step} is not an earlier dependency.")
                    break
                if ref_field and source.produces != ref_field:
                    invalid_refs.add(step.id)
                    plan.gaps.append(
                        f"Dropped {step.id}: ${ref_step}.{ref_field} is not produced by {ref_step}.")
                    break
        dropped |= invalid_refs
        dropped = self._drop_dependents(plan, dropped, "depends on an unresolvable output")
        for step in plan.steps:
            if step.id in dropped:
                step.status = "skipped"
                step.error = "validation: dropped before execution"
                plan.dropped_steps.append(step)
        plan.steps = [s for s in plan.steps if s.id not in dropped]

        denied: set[str] = set()
        for step in plan.steps:
            step.tier = self.registry.action_tier(step.skill, step.action)
            if step.tier != TIER_HIGH:
                try:
                    if self.approvals.decision_for(self._permission_key(step)) == ALWAYS_DENY:
                        denied.add(step.id)
                        plan.gaps.append(f"Skipped {step.id}: this action pattern is always denied.")
                except Exception:  # noqa: BLE001 - absent permission storage means ASK, not allow
                    pass
        if denied:
            denied = self._drop_dependents(plan, denied, "depends on a denied step")
            for step in plan.steps:
                if step.id in denied:
                    step.status = "skipped"
                    step.error = "validation: denied before execution"
                    plan.dropped_steps.append(step)
            plan.steps = [s for s in plan.steps if s.id not in denied]
        return plan

    @staticmethod
    def _drop_dependents(plan: Plan, dropped: set[str], reason: str) -> set[str]:
        changed = True
        while changed:
            changed = False
            for step in plan.steps:
                if step.id not in dropped and any(dep in dropped for dep in step.depends_on):
                    dropped.add(step.id)
                    plan.gaps.append(f"Dropped {step.id}: {reason}.")
                    changed = True
        return dropped

    @staticmethod
    def _topological(steps: Iterable[PlanStep]) -> list[PlanStep]:
        ordered_input = list(steps)
        by_id = {s.id: s for s in ordered_input}
        indegree = {s.id: len(s.depends_on) for s in ordered_input}
        ready = [s.id for s in ordered_input if indegree[s.id] == 0]
        out: list[PlanStep] = []
        while ready:
            sid = ready.pop(0)
            out.append(by_id[sid])
            for step in ordered_input:
                if sid in step.depends_on:
                    indegree[step.id] -= 1
                    if indegree[step.id] == 0:
                        ready.append(step.id)
        if len(out) != len(ordered_input):
            raise PlanValidationError("plan dependencies contain a cycle")
        return out

    @staticmethod
    def _ancestors(steps: list[PlanStep]) -> dict[str, set[str]]:
        by_id = {s.id: s for s in steps}
        memo: dict[str, set[str]] = {}

        def visit(sid: str) -> set[str]:
            if sid not in memo:
                direct = set(by_id[sid].depends_on)
                memo[sid] = direct | set().union(*(visit(d) for d in direct)) if direct else set()
            return memo[sid]

        return {sid: visit(sid) for sid in by_id}

    @classmethod
    def _references(cls, value: Any) -> list[tuple[str, str | None]]:
        out: list[tuple[str, str | None]] = []
        if isinstance(value, str):
            match = _REF_RE.fullmatch(value.strip())
            if match:
                out.append((match.group("step"), match.group("field")))
        elif isinstance(value, dict):
            for child in value.values():
                out.extend(cls._references(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                out.extend(cls._references(child))
        return out

    # -------------------------------------------------------------- approvals
    @staticmethod
    def _permission_key(step: PlanStep) -> str:
        return f"plan:{step.skill}.{step.action}"

    def needs_approval(self, plan: Plan) -> bool:
        for step in plan.steps:
            if step.status == "skipped" or step.tier == TIER_TRIVIAL:
                continue
            if step.tier == TIER_HIGH:
                return True
            try:
                if self.approvals.decision_for(self._permission_key(step)) != ALWAYS_APPROVE:
                    return True
            except Exception:  # noqa: BLE001 - fail closed when permission storage is unavailable
                return True
        return False

    def preview(self, plan: Plan) -> str:
        lines = [f"Plan {plan.id}: {plan.goal}"]
        if plan.steps:
            for n, step in enumerate(self._topological(plan.steps), 1):
                why = f" — {step.rationale}" if step.rationale else ""
                lines.append(f"{n}. [{step.tier}] {step.skill}.{step.action}{why}")
        else:
            lines.append(plan.reason or "No available capability can satisfy this goal.")
        if plan.gaps:
            lines.append("Gaps:")
            lines.extend(f"- {gap}" for gap in plan.gaps)
        if plan.status == "awaiting_approval":
            lines.append("Reply 'yes' to run it, 'skip N' to remove a step, or 'abort'.")
        return "\n".join(lines)

    # --------------------------------------------------------------- lifecycle
    def run(self, goal: str, *, channel: str = "cli", dry_run: bool = False) -> CommandResult:
        try:
            plan = self.validate_plan(self.plan(goal, channel=channel))
        except (LLMError, PlanValidationError, ValueError) as exc:
            log.warning("could not build plan: %s", exc)
            return CommandResult(text=f"I couldn't work out a safe plan for that: {exc}",
                                 data={"planned": False}, ok=False)
        if not plan.steps:
            plan.status = "failed"
            self._persist_plan(plan)
            return CommandResult(text=self.preview(plan),
                                 data={"plan_id": plan.id, "steps": 0}, ok=False)
        if dry_run:
            return CommandResult(text=self.preview(plan),
                                 data={"plan_id": plan.id, "dry_run": True})
        if self.needs_approval(plan):
            plan.status = "awaiting_approval"
            self._persist_plan(plan)
            return CommandResult(text=self.preview(plan), data={
                "plan_id": plan.id, "awaiting_approval": True,
                "steps": [asdict(s) for s in plan.steps],
            })
        self._persist_plan(plan)
        return self.execute_plan(plan)

    def execute_plan(self, plan: Plan) -> CommandResult:
        plan.status = "executing"
        self._persist_plan(plan)
        for step in self._topological(plan.steps):
            if step.status in {"done", "skipped"}:
                continue
            if any(self._step(plan, dep).status in {"failed", "skipped"}
                   for dep in step.depends_on):
                step.status = "skipped"
                step.error = "dependency did not complete"
                self._persist_step(plan, step)
                continue
            step.status = "running"
            step.attempts += 1
            self._persist_step(plan, step)
            try:
                args = self._resolve_args(step.args, plan)
                if (hasattr(self.registry, "is_queued_action")
                        and self.registry.is_queued_action(step.skill, step.action)):
                    from core.queue import get_queue

                    queue_id = get_queue().enqueue(
                        "plan.step",
                        {"plan_id": plan.id, "step_id": step.id, "args": args},
                        dedupe_key=f"plan:{plan.id}:{step.id}:{step.attempts}",
                    )
                    step.output = {"queue_id": queue_id}
                    self._persist_step(plan, step)
                    self._persist_plan(plan)
                    return CommandResult(
                        text=(f"Plan {plan.id} is running in the background at {step.id} "
                              f"({step.skill}.{step.action}), queue #{queue_id}."),
                        data={"plan_id": plan.id, "status": "executing",
                              "queued_step": step.id, "queue_id": queue_id},
                    )
                result = self.registry.dispatch_intent(Intent(
                    name=f"plan:{step.id}", skill=step.skill, action=step.action,
                    args=args, confidence=1.0, via="plan",
                ))
                if not result.ok:
                    raise RuntimeError(result.text)
                step.output = {**self._json_object(result.data),
                               "_text": result.text, "_ok": result.ok}
                step.status = "done"
                step.error = None
                self._persist_step(plan, step)
            except Exception as exc:  # noqa: BLE001 - pause is the safety behavior
                step.status = "failed"
                step.error = str(exc)[:2000]
                plan.status = "paused"
                self._persist_step(plan, step)
                self._persist_plan(plan)
                return CommandResult(
                    text=(f"Plan {plan.id} paused at {step.id} ({step.skill}.{step.action}): "
                          f"{step.error}\nReply 'continue', 'skip', or 'abort'."),
                    data={"plan_id": plan.id, "paused": True, "failed_step": step.id}, ok=False,
                )
        plan.status = "done"
        self._persist_plan(plan)
        completed = [s for s in plan.steps if s.status == "done"]
        lines = [f"Completed plan {plan.id}: {plan.goal}"]
        lines.extend(f"- {s.output.get('_text', s.id) if s.output else s.id}" for s in completed)
        skipped = [s.id for s in plan.steps if s.status == "skipped"]
        if skipped:
            lines.append("Skipped: " + ", ".join(skipped))
        return CommandResult(text="\n".join(lines), data={
            "plan_id": plan.id, "status": plan.status,
            "outputs": {s.id: s.output for s in completed},
        })

    def execute_queued_step(self, plan_id: str, step_id: str,
                            args: dict[str, Any]) -> CommandResult:
        """Worker callback: finish one heavy step, persist output, then advance the DAG."""
        plan = self.load_plan(plan_id)
        if plan is None:
            return CommandResult(text=f"Plan {plan_id} was not found.", ok=False)
        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            return CommandResult(text=f"Plan {plan_id} has no step {step_id}.", ok=False)
        if step.status == "done":
            return CommandResult(text=f"Plan {plan_id} step {step_id} was already done.")
        try:
            result = self.registry.dispatch_intent(Intent(
                name=f"plan:{step.id}", skill=step.skill, action=step.action,
                args=args, confidence=1.0, via="plan",
            ))
            if not result.ok:
                raise RuntimeError(result.text)
            step.output = {**self._json_object(result.data),
                           "_text": result.text, "_ok": result.ok}
            step.status, step.error = "done", None
            self._persist_step(plan, step)
            return self.execute_plan(plan)
        except Exception as exc:  # noqa: BLE001
            step.status, step.error = "failed", str(exc)[:2000]
            plan.status = "paused"
            self._persist_step(plan, step)
            self._persist_plan(plan)
            return CommandResult(
                text=(f"Plan {plan.id} paused at queued step {step.id}: {step.error}. "
                      "Reply 'continue', 'skip', or 'abort'."),
                data={"plan_id": plan.id, "paused": True, "failed_step": step.id}, ok=False,
            )

    def handle_reply(self, text: str) -> CommandResult | None:
        """Consume only unambiguous replies while a plan is awaiting or paused."""
        plan = self.current_plan()
        if plan is None:
            return None
        answer = (text or "").strip()
        skip = _SKIP_RE.fullmatch(answer)
        if plan.status == "awaiting_approval":
            if _ABORT_RE.fullmatch(answer):
                plan.status = "cancelled"
                self._persist_plan(plan)
                return CommandResult(text=f"Cancelled plan {plan.id}.",
                                     data={"plan_id": plan.id, "status": plan.status})
            if skip:
                if not self._skip_step(plan, skip.group("which")):
                    return CommandResult(text="Which step should I skip? Use 'skip 2' or its step id.",
                                         ok=False)
                if self.needs_approval(plan):
                    plan.status = "awaiting_approval"
                    self._persist_plan(plan)
                    return CommandResult(text=self.preview(plan), data={"plan_id": plan.id})
                return self.execute_plan(plan)
            if _YES_RE.fullmatch(answer):
                return self.execute_plan(plan)
            return None
        if plan.status == "paused":
            if _ABORT_RE.fullmatch(answer):
                plan.status = "cancelled"
                self._persist_plan(plan)
                return CommandResult(text=f"Cancelled plan {plan.id}.",
                                     data={"plan_id": plan.id, "status": plan.status})
            if skip:
                failed = next((s for s in plan.steps if s.status == "failed"), None)
                which = skip.group("which") or (failed.id if failed else None)
                if not self._skip_step(plan, which):
                    return CommandResult(text="I couldn't identify the failed step to skip.", ok=False)
                return self.execute_plan(plan)
            if _RESUME_RE.fullmatch(answer):
                for step in plan.steps:
                    if step.status == "failed":
                        step.status, step.error = "pending", None
                        self._persist_step(plan, step)
                        break
                return self.execute_plan(plan)
        return None

    def resume(self, plan_id: str) -> CommandResult:
        plan = self.load_plan(plan_id)
        if plan is None:
            return CommandResult(text=f"Plan {plan_id} was not found.", ok=False)
        if plan.status == "awaiting_approval":
            return CommandResult(text=self.preview(plan), data={"plan_id": plan.id}, ok=False)
        for step in plan.steps:
            if step.status in {"failed", "running"}:
                step.status, step.error = "pending", None
        return self.execute_plan(plan)

    def _skip_step(self, plan: Plan, which: str | None) -> bool:
        if not which:
            return False
        step = next((s for s in plan.steps if s.id == which), None)
        if step is None and which.isdigit():
            n = int(which)
            ordered = self._topological(plan.steps)
            step = ordered[n - 1] if 1 <= n <= len(ordered) else None
        if step is None or step.status == "done":
            return False
        step.status = "skipped"
        step.error = "skipped by Calvin"
        self._persist_step(plan, step)
        return True

    @staticmethod
    def _step(plan: Plan, step_id: str) -> PlanStep:
        return next(s for s in plan.steps if s.id == step_id)

    def _resolve_args(self, value: Any, plan: Plan) -> Any:
        if isinstance(value, str):
            match = _REF_RE.fullmatch(value.strip())
            if not match:
                return value
            source = self._step(plan, match.group("step"))
            if source.output is None:
                raise ValueError(f"{source.id} has no output")
            field_name = match.group("field")
            if not field_name:
                return source.output
            if field_name in source.output:
                return source.output[field_name]
            if source.produces == field_name:
                public = {k: v for k, v in source.output.items() if not k.startswith("_")}
                return next(iter(public.values())) if len(public) == 1 else public
            raise ValueError(f"{source.id} output has no field {field_name!r}")
        if isinstance(value, dict):
            return {k: self._resolve_args(v, plan) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_args(v, plan) for v in value]
        return value

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"value": str(value)}
        # Round-trip with default=str so Path/datetime-like skill outputs remain persistable.
        return json.loads(json.dumps(value, default=str))

    # ------------------------------------------------------------- persistence
    def _persist_plan(self, plan: Plan) -> None:
        self.memory.save_plan({
            "id": plan.id, "goal": plan.goal, "status": plan.status,
            "channel": plan.channel, "session_id": plan.session_id,
            "reason": plan.reason, "gaps": plan.gaps,
        })
        for step in plan.steps:
            self._persist_step(plan, step)
        for step in plan.dropped_steps:
            self._persist_step(plan, step)

    def _persist_step(self, plan: Plan, step: PlanStep) -> None:
        self.memory.save_plan_step(plan.id, asdict(step))

    def load_plan(self, plan_id: str) -> Plan | None:
        raw = self.memory.get_plan(plan_id)
        return self._from_record(raw) if raw else None

    def current_plan(self) -> Plan | None:
        raw = self.memory.current_plan(self.session_id)
        return self._from_record(raw) if raw else None

    @staticmethod
    def _from_record(raw: dict[str, Any]) -> Plan:
        all_steps = [PlanStep(**{
            key: row.get(key) for key in PlanStep.__dataclass_fields__
            if key in row
        }) for row in raw.get("steps", [])]
        dropped = [s for s in all_steps
                   if s.status == "skipped" and (s.error or "").startswith("validation:")]
        steps = [s for s in all_steps if s not in dropped]
        return Plan(
            id=str(raw["id"]), goal=str(raw["goal"]), steps=steps,
            status=str(raw.get("status", "planning")), channel=str(raw.get("channel", "cli")),
            session_id=str(raw.get("session_id", "calvin")),
            reason=str(raw.get("reason", "") or ""), gaps=list(raw.get("gaps") or []),
            dropped_steps=dropped,
        )
