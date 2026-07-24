"""Weather (Phase 36 Slice 3): Open-Meteo, keyless, cached 30 minutes per location. The
property that matters most: a network failure produces an honest "I couldn't reach the
weather service", never an invented forecast (§0 P5). Voice and HUD render from the same
fetched dict, so this only needs to prove the dict is right — both consumers follow for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from core.persona_store import PersonaEngine
from skills.weather import WeatherSkill, describe_code, extract_override, voice_line

GEO_OK = {"results": [{"name": "Nairobi", "latitude": -1.28, "longitude": 36.82}]}
FORECAST_OK = {
    "current": {"temperature_2m": 22.4, "weather_code": 3, "wind_speed_10m": 8.1},
    "daily": {"temperature_2m_max": [25.0], "temperature_2m_min": [16.0]},
}


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
    """Dispatches on which Open-Meteo endpoint was hit, tracks call count."""

    def __init__(self, geo=GEO_OK, forecast=FORECAST_OK, fail: Exception | None = None):
        self.geo, self.forecast, self.fail = geo, forecast, fail
        self.calls = 0

    def __call__(self, url: str, params: dict | None = None, **kw: Any):
        self.calls += 1
        if self.fail:
            raise self.fail
        if "geocoding" in url:
            return _FakeResponse(self.geo)
        return _FakeResponse(self.forecast)


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _skill(mem, fetch=None, persona=None, clock=None) -> WeatherSkill:
    return WeatherSkill(memory=mem, persona=persona, fetch=fetch or _FakeFetch(),
                        clock=clock or _Clock())


# ================================================================= WMO codes
def test_known_code_describes_conditions():
    assert describe_code(3) == "overcast"


def test_unknown_code_degrades_to_a_generic_label_not_a_guess():
    assert describe_code(9999) == "changing conditions"


def test_non_numeric_code_does_not_crash():
    assert describe_code(None) == "changing conditions"
    assert describe_code("garbage") == "changing conditions"


# ================================================================= override parsing
@pytest.mark.parametrize("text, expected", [
    ("weather in Mombasa", "Mombasa"),
    ("what's the weather for Kisumu?", "Kisumu"),
    ("weather at Nakuru.", "Nakuru"),
    ("how's it looking outside", None),
])
def test_extract_override(text, expected):
    assert extract_override(text) == expected


# ================================================================= location resolution
def test_no_city_and_no_default_asks(mem):
    skill = _skill(mem, persona=PersonaEngine(llm=None, memory=mem))
    result = skill.current()
    assert result.ok is False
    assert "city" in result.text.lower()


def test_default_city_comes_from_a_verified_persona_fact(mem):
    persona = PersonaEngine(llm=None, memory=mem)
    persona.add_fact("bio", "city", "Nairobi", verified=True)
    fetch = _FakeFetch()
    skill = _skill(mem, fetch=fetch, persona=persona)
    result = skill.current()
    assert result.ok is True
    assert result.data["weather"]["city"] == "Nairobi"


def test_spoken_override_beats_the_default(mem):
    persona = PersonaEngine(llm=None, memory=mem)
    persona.add_fact("bio", "city", "Nairobi", verified=True)
    fetch = _FakeFetch(geo={"results": [{"name": "Mombasa", "latitude": -4.0, "longitude": 39.6}]})
    skill = _skill(mem, fetch=fetch, persona=persona)
    result = skill.current(text="weather in Mombasa")
    assert result.data["weather"]["city"] == "Mombasa"


def test_explicit_city_argument_wins_over_everything(mem):
    fetch = _FakeFetch(geo={"results": [{"name": "Kisumu", "latitude": -0.1, "longitude": 34.75}]})
    skill = _skill(mem, fetch=fetch, persona=PersonaEngine(llm=None, memory=mem))
    result = skill.current(city="Kisumu")
    assert result.data["weather"]["city"] == "Kisumu"


# ================================================================= honesty on failure
def test_place_not_found_is_an_honest_message_not_a_guess(mem):
    fetch = _FakeFetch(geo={"results": []})
    skill = _skill(mem, fetch=fetch, persona=PersonaEngine(llm=None, memory=mem))
    result = skill.current(city="Nowhereville")
    assert result.ok is False
    assert "couldn't find" in result.text.lower()


def test_network_failure_degrades_honestly_never_fabricates(mem):
    fetch = _FakeFetch(fail=ConnectionError("no route to host"))
    skill = _skill(mem, fetch=fetch, persona=PersonaEngine(llm=None, memory=mem))
    result = skill.current(city="Nairobi")
    assert result.ok is False
    assert "couldn't reach" in result.text.lower()


# ================================================================= caching
def test_second_call_within_the_window_hits_cache_not_the_network(mem):
    clock = _Clock()
    fetch = _FakeFetch()
    persona = PersonaEngine(llm=None, memory=mem)
    skill = _skill(mem, fetch=fetch, persona=persona, clock=clock)
    skill.current(city="Nairobi")
    calls_after_first = fetch.calls
    clock.t += 60  # one minute later, well inside the 30-minute cache
    skill.current(city="Nairobi")
    assert fetch.calls == calls_after_first, "a cached lookup must not hit the network again"


def test_cache_expires_after_the_window(mem):
    clock = _Clock()
    fetch = _FakeFetch()
    persona = PersonaEngine(llm=None, memory=mem)
    skill = _skill(mem, fetch=fetch, persona=persona, clock=clock)
    skill.current(city="Nairobi")
    calls_after_first = fetch.calls
    clock.t += 31 * 60  # past the 30-minute cache window
    skill.current(city="Nairobi")
    assert fetch.calls > calls_after_first, "an expired cache entry must refetch"


# ================================================================= voice line
def test_voice_line_includes_high_and_low_when_present():
    line = voice_line({"city": "Nairobi", "condition": "overcast", "temperature": 22.4,
                       "high": 25.0, "low": 16.0})
    assert "Nairobi" in line and "overcast" in line and "25" in line and "16" in line


def test_voice_line_degrades_gracefully_with_partial_data():
    line = voice_line({"city": "Nairobi", "condition": "overcast"})
    assert "Nairobi" in line
    assert line.strip().endswith(".")
