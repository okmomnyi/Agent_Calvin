"""skills/stage.py: voice-steering pseudo-skill (Phase 38). Focus resolves markets vs
world_news by delegating to each skill's own resolver -- no new taxonomy, no fabrication;
pin/unpin/idle are plain signals with no server-side state to change."""

from __future__ import annotations

from skills.stage import StageSkill


def test_focus_resolves_a_known_market_asset():
    result = StageSkill().focus(subject="oil")
    assert result.ok
    assert result.data == {"stage_kind": "markets", "asset": "oil"}


def test_focus_resolves_a_known_news_topic():
    result = StageSkill().focus(subject="kenya")
    assert result.ok
    assert result.data == {"stage_kind": "world_news", "topic": "kenya"}


def test_focus_prefers_markets_when_a_word_could_plausibly_be_either():
    """"gold" is a configured commodity instrument and not a news category alias -- must
    resolve to markets, not accidentally fall through to news."""
    result = StageSkill().focus(subject="gold")
    assert result.data["stage_kind"] == "markets"


def test_focus_with_an_unresolvable_subject_is_honest_about_it():
    result = StageSkill().focus(subject="underwater basket weaving")
    assert result.ok is False
    assert result.data == {"stage_kind": "none"}


def test_focus_with_a_blank_subject_asks_for_clarification():
    result = StageSkill().focus(subject="")
    assert result.ok is False
    assert result.data == {"stage_kind": "none"}


def test_pin_and_unpin_are_pure_signals_with_no_extra_state():
    pin = StageSkill().pin()
    unpin = StageSkill().unpin()
    assert pin.data == {"stage_kind": "pin"}
    assert unpin.data == {"stage_kind": "unpin"}
    assert pin.ok and unpin.ok


def test_idle_signals_the_presenter_explicitly():
    result = StageSkill().idle()
    assert result.ok
    assert result.data == {"stage_kind": "idle"}


def test_contract_reads_no_standing_instruction_categories():
    assert StageSkill().contract().reads_categories == []


def test_commands_expose_all_four_actions():
    assert set(StageSkill().commands().keys()) == {"focus", "pin", "unpin", "idle"}
