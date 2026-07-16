"""Weekly timetable loader for the semester planner (Phase 13).

Reads config/timetable.yaml (weekly grid + one-off events + unit-code names) and returns
the classes for a given day. Pure and date-injectable so the morning briefing is testable
without depending on the real clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.timetable")

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _path() -> Path:
    return get_settings().project_root / "config" / "timetable.yaml"


def load(path: Path | None = None) -> dict[str, Any]:
    p = path or _path()
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def unit_names(data: dict[str, Any] | None = None) -> dict[str, str]:
    data = data if data is not None else load()
    return dict(data.get("units", {}))


def classes_on(weekday_index: int, date_iso: str = "", data: dict[str, Any] | None = None
               ) -> list[dict[str, Any]]:
    """Return classes for a weekday (0=Mon … 6=Sun), merged with any one-off events on date_iso."""
    data = data if data is not None else load()
    day = _WEEKDAYS[weekday_index % 7]
    classes = list((data.get("weekly", {}) or {}).get(day, []) or [])
    if date_iso:
        for ev in data.get("one_off", []) or []:
            if ev.get("date") == date_iso:
                classes.append(ev)
    classes.sort(key=lambda c: c.get("start", "99:99"))
    return classes
