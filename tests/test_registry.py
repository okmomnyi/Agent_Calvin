"""kernel/registry.py's route()/ack_for() (Phase 40): resolve-without-dispatch, and the
ack lookup a channel uses before actually running a command.
"""

from __future__ import annotations

from core.intent import Intent, IntentRouter
from core.skill import BaseSkill, CommandResult
from kernel.registry import SkillRegistry


class _EchoSkill(BaseSkill):
    name = "echo"
    acks = {"say": ("Right away. Echoing…", "moment")}
    ran = False

    def commands(self):
        return {"say": self.say}

    def say(self, **_):
        self.ran = True
        return CommandResult(text="said it")


def _registry(fake_llm) -> SkillRegistry:
    reg = SkillRegistry(router=IntentRouter(llm=fake_llm))
    reg.discover()
    return reg


def test_route_uses_the_fast_keyword_path_without_calling_the_llm(fake_llm):
    reg = _registry(fake_llm)
    intent = reg.route("check my email", use_llm=True)
    assert intent.skill == "email_agent" and intent.action == "check"
    assert fake_llm.calls == []  # keyword hit -- never reached the LLM at all


def test_route_falls_back_to_the_full_router_when_keyword_misses(fake_llm):
    fake_llm.classify_result = "find_jobs"
    reg = _registry(fake_llm)
    intent = reg.route("is there anything worth applying to today", use_llm=True)
    assert intent.name == "find_jobs"
    assert intent.via == "llm"


def test_route_never_dispatches_anything():
    echo = _EchoSkill()
    reg = SkillRegistry(router=IntentRouter(llm=None))
    reg.register(echo)
    reg.route("say hello", use_llm=False)
    assert echo.ran is False  # resolve-only -- no side effect


def test_ack_for_returns_the_skills_declared_ack():
    echo = _EchoSkill()
    reg = SkillRegistry(router=IntentRouter(llm=None))
    reg.register(echo)
    intent = Intent(name="say", skill="echo", action="say")
    assert reg.ack_for(intent) == ("Right away. Echoing…", "moment")


def test_ack_for_falls_back_to_the_default_for_an_undeclared_action():
    echo = _EchoSkill()
    reg = SkillRegistry(router=IntentRouter(llm=None))
    reg.register(echo)
    intent = Intent(name="x", skill="echo", action="some_other_action")
    from core.skill import ACK_DEFAULT_LATENCY, ACK_DEFAULT_TEXT

    assert reg.ack_for(intent) == (ACK_DEFAULT_TEXT, ACK_DEFAULT_LATENCY)


def test_ack_for_returns_none_when_the_skill_is_not_registered():
    reg = SkillRegistry(router=IntentRouter(llm=None))
    intent = Intent(name="x", skill="nonexistent_skill", action="whatever")
    assert reg.ack_for(intent) is None


def test_ack_for_degrades_to_none_when_the_skills_own_ack_for_raises():
    class _Boom(BaseSkill):
        name = "boom"

        def ack_for(self, action):
            raise RuntimeError("broken skill")

    reg = SkillRegistry(router=IntentRouter(llm=None))
    reg.register(_Boom())
    intent = Intent(name="x", skill="boom", action="whatever")
    assert reg.ack_for(intent) is None
