"""YouTube (Phase 36 Slice 3).

No network, no pywhatkit, no browser automation: every "open a URL" call goes through an
injected opener (the real one is skills/web_open.py), and the optional Data API call is
injected too. Absent YOUTUBE_API_KEY, or on any API failure, this must fall back to a
search URL rather than guess a video id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.skill import CommandResult
from skills.youtube import YouTubeSkill, clean_query


class _FakeOpener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, url: str = "", **_: Any) -> CommandResult:
        self.calls.append(url)
        return CommandResult(text="ok", data={"client_actions": [{"op": "open_url", "url": url}]})


@dataclass
class _FakeResponse:
    payload: dict
    status: int = 200

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self) -> dict:
        return self.payload


class _FakeFetch:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, url: str, params: dict | None = None, **kw: Any):
        self.calls.append({"url": url, "params": params})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ================================================================= query cleaning
@pytest.mark.parametrize("raw, expected", [
    ("play bohemian rhapsody", "bohemian rhapsody"),
    ("search for lofi hip hop please", "lofi hip hop"),
    ("find some jazz on youtube", "some jazz"),
    ("Whats due this week", "Whats due this week"),  # not a youtube-shaped phrase; passthrough
])
def test_clean_query_strips_verbs_and_filler(raw, expected):
    assert clean_query(raw) == expected


def test_clean_query_of_nothing_is_empty():
    assert clean_query("play") == ""
    assert clean_query("") == ""


# ================================================================= no API key -> search
def test_no_key_falls_back_to_search(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    opener = _FakeOpener()
    skill = YouTubeSkill(opener=opener)
    result = skill.play(query="play some jazz")
    assert result.ok is True
    assert opener.calls == ["https://www.youtube.com/results?search_query=some+jazz"]


def test_empty_query_asks_rather_than_searching_for_nothing(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    opener = _FakeOpener()
    skill = YouTubeSkill(opener=opener)
    result = skill.play(query="play")
    assert result.ok is False
    assert opener.calls == []


# ================================================================= API key path
def test_with_key_and_a_result_opens_the_video_directly(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    opener = _FakeOpener()
    fetch = _FakeFetch(_FakeResponse({"items": [{"id": {"videoId": "abc123"}}]}))
    skill = YouTubeSkill(opener=opener, fetch=fetch)
    result = skill.play(query="play some jazz")
    assert result.ok is True
    assert opener.calls == ["https://www.youtube.com/watch?v=abc123"]
    assert fetch.calls[0]["params"]["key"] == "test-key"


def test_with_key_but_no_results_falls_back_to_search(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    opener = _FakeOpener()
    fetch = _FakeFetch(_FakeResponse({"items": []}))
    skill = YouTubeSkill(opener=opener, fetch=fetch)
    result = skill.play(query="play some obscure track")
    assert result.ok is True
    assert "results?search_query=" in opener.calls[0]


def test_with_key_but_api_error_falls_back_to_search_not_a_crash(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    opener = _FakeOpener()
    fetch = _FakeFetch(RuntimeError("network down"))
    skill = YouTubeSkill(opener=opener, fetch=fetch)
    result = skill.play(query="play something")
    assert result.ok is True
    assert "results?search_query=" in opener.calls[0]
