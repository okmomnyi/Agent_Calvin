"""core/skill.py: the ack_for() mechanism every skill inherits (Phase 40).

The one router-level acknowledgement primitive -- a skill declares its own wording via
the `acks` dict; everything not declared falls back to the plain generic default.
"""

from __future__ import annotations

from core.skill import ACK_DEFAULT_LATENCY, ACK_DEFAULT_TEXT, BaseSkill


class _PlainSkill(BaseSkill):
    name = "plain"


class _CustomAckSkill(BaseSkill):
    name = "custom"
    acks = {
        "playlist": ("Right away. Cueing that up…", "moment"),
        "start_session": ("On it — starting the session…", "long"),
    }


def test_default_ack_for_a_skill_with_no_declared_acks():
    assert _PlainSkill().ack_for("anything") == (ACK_DEFAULT_TEXT, ACK_DEFAULT_LATENCY)


def test_declared_ack_overrides_the_default_for_that_action():
    skill = _CustomAckSkill()
    assert skill.ack_for("playlist") == ("Right away. Cueing that up…", "moment")


def test_an_action_not_in_the_acks_dict_still_falls_back_to_the_default():
    skill = _CustomAckSkill()
    assert skill.ack_for("some_other_action") == (ACK_DEFAULT_TEXT, ACK_DEFAULT_LATENCY)


def test_a_long_latency_ack_is_declared_explicitly():
    skill = _CustomAckSkill()
    text, latency = skill.ack_for("start_session")
    assert latency == "long"
    assert text  # non-empty


def test_acks_dict_does_not_leak_between_skill_classes():
    """A class-level mutable default is the classic footgun -- confirm _PlainSkill's
    acks dict (inherited from BaseSkill) is unaffected by _CustomAckSkill's overrides."""
    assert _PlainSkill().ack_for("playlist") == (ACK_DEFAULT_TEXT, ACK_DEFAULT_LATENCY)
