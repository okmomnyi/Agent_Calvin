"""Seed-data detection (Phase 36 diagnostic fix, #20).

Telegram log: "Example Co" and "Local tech hub" -- config.yaml's own shipped placeholder
text for `planner.commitments` -- reached every single morning briefing because nothing
ever flagged that the example was never replaced. seed_data_warnings() is the check that
makes this visible at startup and in /api/health instead of silently persisting forever.
"""

from __future__ import annotations

from core.config import seed_data_warnings


class _Settings:
    def __init__(self, commitments):
        self._commitments = commitments

    def get(self, *keys, default=None):
        return self._commitments if keys == ("planner", "commitments") else default


def test_shipped_example_commitments_are_flagged():
    settings = _Settings(["Example Co — client web app", "Local tech hub — volunteer"])
    warnings = seed_data_warnings(settings)
    assert len(warnings) == 1
    assert "Example Co" in warnings[0]


def test_real_commitments_are_not_flagged():
    settings = _Settings(["Acme Corp — backend contract", "Nairobi hackerspace — mentor"])
    assert seed_data_warnings(settings) == []


def test_no_commitments_configured_is_not_flagged():
    """Empty is honest; only the specific shipped placeholder text is a problem."""
    settings = _Settings([])
    assert seed_data_warnings(settings) == []


def test_a_mix_of_real_and_placeholder_flags_only_the_placeholder():
    settings = _Settings(["Example Co — client web app", "Acme Corp — real client"])
    warnings = seed_data_warnings(settings)
    assert len(warnings) == 1
    assert "Example Co" in warnings[0] and "Acme Corp" not in warnings[0]
