"""Category-aware job scoring.

Scores a RawJob 0-100 against Calvin's target profile (config.yaml jobs section) with
category-aware guidance: transcription is primary, cloud/DevOps is scored generously
(current skill-growth focus), paid internships count, unpaid roles are auto-skipped.
Uses the cheap `classify`-class model via chat_json for high-volume, structured output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.config import get_settings
from core.llm import LLMClient, LLMError
from core.logging_setup import get_logger
from skills.job_hunter.sources.base import RawJob

log = get_logger("job_hunter.scoring")

_SCHEMA = ('{"score": int 0-100, "category": one of '
           '["transcription","cloud_devops","internship","other"], '
           '"reason": short string, "unpaid": bool}')

# Hard, deterministic pre-LLM filters. The model's own `unpaid` self-report is a soft signal
# that has already missed real cases in production ("Equity-Only" scored 85 because the model
# never flagged it) -- these run BEFORE scoring, so a bad phrasing can't slip past a judgment
# call the model was never reliably making anyway. Cheaper too: a rejected job skips the LLM
# call entirely.
_UNPAID_RE = re.compile(r"\b(equity[- ]only|unpaid|volunteer(?:-based)?|no\s+salary)\b", re.I)

_LANGUAGE_NAMES = ("spanish", "french", "german", "portuguese", "italian", "mandarin",
                   "chinese", "arabic", "japanese", "korean", "russian", "hindi", "dutch",
                   "polish", "turkish")
_LANG_ALTERNATION = "|".join(_LANGUAGE_NAMES)
# "fluent in Spanish", "must speak French" ...
_LANGUAGE_REQUIRE_RE = re.compile(
    rf"\b(?:fluent in|native|must speak|requires?|speaking)\s+({_LANG_ALTERNATION})\b", re.I)
# "... QC Specialist — Spanish (Latin America)", "French (Canada) ..."
_LANGUAGE_LABEL_RE = re.compile(rf"\b({_LANG_ALTERNATION})\s*\([\w\s]+\)", re.I)

# Deterministic, in code, un-arguable by a model — Director/VP/Head/Chief and
# Principal/Staff/Senior-Manager roles are penalized hard rather than trusting the LLM to
# subtract points for a seniority mismatch it has already correctly IDENTIFIED in its own
# `reason` text without ever acting on it. Checked in order; the first match wins, so
# "Senior Director" is caught by the director pattern (0.1), not the weaker `senior` one.
_SENIORITY_MULTIPLIERS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\b(director|vp|vice president|head of|chief)\b", re.I), 0.1),
    (re.compile(r"\b(principal|staff|senior manager)\b", re.I), 0.2),
    (re.compile(r"\bsenior\b", re.I), 0.5),
    (re.compile(r"\b(graduate|junior|associate|intern(?:ship)?|entry[- ]level)\b", re.I), 1.2),
)


def seniority_multiplier(title: str) -> float:
    """1.0 (no signal) unless a seniority keyword in the TITLE says otherwise."""
    for pattern, multiplier in _SENIORITY_MULTIPLIERS:
        if pattern.search(title or ""):
            return multiplier
    return 1.0


def _known_languages() -> set[str]:
    langs = get_settings().get("jobs", "known_languages", default=["english", "swahili"])
    return {str(v).strip().lower() for v in langs}


def hard_reject(job: RawJob) -> str | None:
    """A reason to reject before scoring even runs, or None to proceed."""
    blob = f"{job.title} {job.description}"
    unpaid = _UNPAID_RE.search(blob)
    if unpaid:
        return f"unpaid/equity-only ({unpaid.group(0)})"
    known = _known_languages()
    for pattern in (_LANGUAGE_REQUIRE_RE, _LANGUAGE_LABEL_RE):
        m = pattern.search(blob)
        if m and m.group(1).lower() not in known:
            return f"requires {m.group(1)}, not in known languages {sorted(known)}"
    return None


@dataclass
class Score:
    score: int
    category: str
    reason: str
    unpaid: bool = False


def _profile_lists() -> tuple[list, list, list]:
    """Primary/secondary/also — from MEMORY if Calvin has overridden them, else config.

    The config lists are the seed, not the source of truth: a runtime override in the kv table
    (set via `/profile primary ...`) wins, so his focus can shift without a redeploy. This is
    the "should be memory, not hardcoded" point -- the profile follows him.
    """
    s = get_settings()
    primary = s.get("jobs", "primary", default=[])
    secondary = s.get("jobs", "secondary", default=[])
    also = s.get("jobs", "also", default=[])
    try:
        from core.memory import get_memory

        override = get_memory().kv_get("jobs.profile_override")
        if override:
            import json

            data = json.loads(override)
            primary = data.get("primary", primary)
            secondary = data.get("secondary", secondary)
            also = data.get("also", also)
    except Exception:  # noqa: BLE001 - a bad/absent override just means use config
        pass
    return primary, secondary, also


def _profile_text() -> str:
    primary, secondary, also = _profile_lists()
    return (
        "Calvin's target profile:\n"
        f"- PRIMARY (score high): {primary}\n"
        f"- SECONDARY (score GENEROUSLY — his current cloud/DevOps skill-growth focus, "
        f"surface certification-relevant roles even if not a perfect match): {secondary}\n"
        f"- ALSO acceptable: {also}\n"
        "- Unpaid roles => unpaid:true and score 0 (auto-skip).\n"
        "- Cybersecurity/pentest roles may score normally if they match general skills, "
        "but are not a priority category.\n"
        # #25: a PAID INTERNSHIP for a Year-3 student is an excellent, on-level match, not a
        # fallback from a full role he isn't ready for yet -- being a student is the
        # qualification the role is asking for. Score it as generously as SECONDARY when the
        # field lines up, not as an afterthought behind two other tiers.
        "- category=\"internship\" (paid only) should score as generously as SECONDARY when "
        "the field lines up with PRIMARY or SECONDARY: he is a Year-3 student, so being a "
        "student is the qualification the internship is asking for, not a gap."
    )


def score_job(llm: LLMClient, job: RawJob) -> Score:
    """Return a Score for a job. Falls back to a conservative heuristic on LLM failure."""
    rejected = hard_reject(job)
    if rejected:
        cat = job.category_hint or "other"
        return Score(score=0, category=cat, reason=f"hard filter: {rejected}", unpaid=True)

    user = (
        f"{_profile_text()}\n\n"
        f"JOB:\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
        f"Tags: {', '.join(job.tags)}\nHint: {job.category_hint}\n"
        f"Description: {job.description[:1500]}\n\n"
        "Rate 0-100 how well this fits Calvin and pick the best category."
    )
    try:
        data = llm.chat_json(
            "classify",
            [{"role": "system", "content": "You score job postings for a specific candidate. "
                                           "Be decisive and return only JSON."},
             {"role": "user", "content": user}],
            schema_hint=_SCHEMA,
            temperature=0.0,
            max_tokens=200,
        )
    except LLMError:
        log.warning("Scoring LLM failed for '%s' — using heuristic fallback", job.title)
        cat = job.category_hint or "other"
        base = 55 if cat in ("transcription", "cloud_devops", "internship") else 30
        score = max(0, min(100, round(base * seniority_multiplier(job.title))))
        return Score(score=score, category=cat, reason="heuristic fallback (LLM unavailable)")

    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    category = data.get("category") or job.category_hint or "other"
    if category not in ("transcription", "cloud_devops", "internship", "other"):
        category = job.category_hint or "other"
    unpaid = bool(data.get("unpaid", False))
    if unpaid:
        score = 0
    else:
        # Applied AFTER the model's own score, in code: the model's `reason` text already
        # correctly IDENTIFIES a seniority mismatch ("senior level fits associate target")
        # without ever subtracting points for it. This makes that penalty deterministic and
        # untestable-around by the model, rather than a suggestion inside the prompt.
        score = max(0, min(100, round(score * seniority_multiplier(job.title))))
    return Score(score=score, category=category, reason=str(data.get("reason", ""))[:300], unpaid=unpaid)
