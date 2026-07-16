"""SM-2 spaced-repetition scheduling (Phase 11).

A pure, time-free implementation of the SM-2 algorithm so it is trivially testable. The
skill layer supplies the current card state and a grade; this returns the next state
(ease factor, interval in days, lapse count). The due date is computed by the caller as
now + interval_days, keeping this function deterministic and clock-independent.

Grades map to SM-2 quality: Again→1 (fail), Hard→3, Good→4, Easy→5.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_EASE = 1.3
GRADES = ("again", "hard", "good", "easy")
_QUALITY = {"again": 1, "hard": 3, "good": 4, "easy": 5}


@dataclass
class CardState:
    ease: float = 2.5
    interval_days: int = 0
    lapses: int = 0


def schedule(state: CardState, grade: str) -> CardState:
    """Return the next CardState for a review grade. `grade` in GRADES."""
    if grade not in _QUALITY:
        raise ValueError(f"unknown grade {grade!r}; expected one of {GRADES}")
    q = _QUALITY[grade]

    # Failure (Again): relearn tomorrow, drop ease, count a lapse.
    if q < 3:
        return CardState(ease=max(MIN_EASE, round(state.ease - 0.2, 4)),
                         interval_days=1, lapses=state.lapses + 1)

    # Update ease factor (standard SM-2 formula), floored at 1.3.
    ease = max(MIN_EASE, round(state.ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)), 4))

    if state.interval_days <= 0:
        interval = 1
    elif state.interval_days == 1:
        interval = 6
    else:
        interval = round(state.interval_days * ease)

    if grade == "hard":
        interval = max(1, round(interval * 0.8))   # Hard shortens the next interval

    return CardState(ease=ease, interval_days=int(interval), lapses=state.lapses)
