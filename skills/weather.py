"""Weather (Phase 36 Slice 3) — Open-Meteo, keyless and free (§0 P1), for both geocoding and
forecast. No paid provider is ever added.

Default location comes from Calvin's verified persona facts (a `city` or `location` fact);
"weather in Mombasa" overrides it for one call. Cached 30 minutes per location in the
existing kv store (no per-query network hit). A network failure degrades to an honest
"I couldn't reach the weather service" — never a fabricated forecast (§0 P5). The voice
sentence and the HUD panel are built from the exact same fetched `report` dict, so they can
never disagree about what was actually retrieved.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import requests

from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.persona_store import PersonaEngine, get_engine
from core.skill import BaseSkill, CommandResult, SkillContract

log = get_logger("skills.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 30 * 60

# The common subset of WMO weather-interpretation codes Open-Meteo returns. An unlisted code
# degrades to a generic label rather than a guess at what it might mean.
_WMO: dict[int, str] = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}

_OVERRIDE_RE = re.compile(r"\bweather\s+(?:in|at|for)\s+(?P<t>.+)", re.I)


def describe_code(code: Any) -> str:
    try:
        return _WMO.get(int(code), "changing conditions")
    except (TypeError, ValueError):
        return "changing conditions"


def extract_override(text: str) -> str | None:
    m = _OVERRIDE_RE.search(text or "")
    return m.group("t").strip().rstrip(".!?") if m else None


class WeatherError(Exception):
    """A place couldn't be resolved — distinct from a network/transport failure."""


class WeatherSkill(BaseSkill):
    name = "weather"

    def __init__(self, memory: Memory | None = None, persona: PersonaEngine | None = None,
                 fetch: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._persona = persona
        self._fetch = fetch or requests.get
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def persona(self) -> PersonaEngine:
        if self._persona is None:
            self._persona = get_engine()
        return self._persona

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"current": self.current}

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=[])

    # ------------------------------------------------------------- location
    def _default_city(self) -> str | None:
        for fact in self.persona.get_facts(verified_only=True):
            if fact["key"].lower() in ("city", "location"):
                return fact["value"]
        return None

    # ------------------------------------------------------------- fetch + cache
    def _fetch_report(self, city: str) -> dict[str, Any]:
        cache_key = f"weather:{city.strip().lower()}"
        cached = self.mem.kv_get(cache_key)
        if cached:
            try:
                data = json.loads(cached)
                if self._now() - data.get("_cached_at", 0) < CACHE_SECONDS:
                    return data
            except (json.JSONDecodeError, TypeError):
                pass  # a corrupted cache entry is a miss, not a crash
        report = self._fetch_live(city)
        report["_cached_at"] = self._now()
        self.mem.kv_set(cache_key, json.dumps(report))
        return report

    def _fetch_live(self, city: str) -> dict[str, Any]:
        geo = self._fetch(GEOCODE_URL, params={"name": city, "count": 1}, timeout=10)
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            raise WeatherError(f"I couldn't find a place called \"{city}\".")
        place = results[0]
        lat, lon = place["latitude"], place["longitude"]

        fc = self._fetch(FORECAST_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min",
        }, timeout=10)
        fc.raise_for_status()
        data = fc.json()
        current = data.get("current", {})
        daily = data.get("daily", {})
        return {
            "city": place.get("name", city),
            "temperature": current.get("temperature_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "condition": describe_code(current.get("weather_code")),
            "high": (daily.get("temperature_2m_max") or [None])[0],
            "low": (daily.get("temperature_2m_min") or [None])[0],
        }

    # ------------------------------------------------------------- command
    def current(self, city: str = "", text: str = "", **_: Any) -> CommandResult:
        target = city or extract_override(text) or self._default_city()
        if not target:
            return CommandResult(
                text="I don't know where you are yet — tell me your city, or ask "
                     "\"weather in <city>\".", ok=False)
        try:
            report = self._fetch_report(target)
        except WeatherError as exc:
            return CommandResult(text=str(exc), ok=False)
        except Exception as exc:  # noqa: BLE001 - network is unreliable; never fabricate
            log.warning("weather lookup failed for %s: %s", target, exc)
            return CommandResult(text="I couldn't reach the weather service.", ok=False)
        return CommandResult(text=voice_line(report), data={"weather": report})


def voice_line(report: dict[str, Any]) -> str:
    """One spoken sentence — the HUD panel renders the same `report` dict structurally,
    from the same fetch, so voice and screen never disagree."""
    temp = report.get("temperature")
    cond = report.get("condition", "changing conditions")
    high, low = report.get("high"), report.get("low")
    parts = [f"It's {cond} in {report.get('city', 'your area')}"]
    if temp is not None:
        parts[0] += f" and {round(temp)}°C right now"
    if high is not None and low is not None:
        parts.append(f"with a high of {round(high)}°C and a low of {round(low)}°C today")
    return ", ".join(parts) + "."


SKILL = WeatherSkill()
