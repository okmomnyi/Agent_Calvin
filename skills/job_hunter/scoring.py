"""Category-aware job scoring.

Scores a RawJob 0-100 against Calvin's target profile (config.yaml jobs section) with
category-aware guidance: transcription is primary, cloud/DevOps is scored generously
(current skill-growth focus), paid internships count, unpaid roles are auto-skipped.
Uses the cheap `classify`-class model via chat_json for high-volume, structured output.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import get_settings
from core.llm import LLMClient, LLMError
from core.logging_setup import get_logger
from skills.job_hunter.sources.base import RawJob

log = get_logger("job_hunter.scoring")

_SCHEMA = ('{"score": int 0-100, "category": one of '
           '["transcription","cloud_devops","internship","other"], '
           '"reason": short string, "unpaid": bool}')


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
        "but are not a priority category."
    )


def score_job(llm: LLMClient, job: RawJob) -> Score:
    """Return a Score for a job. Falls back to a conservative heuristic on LLM failure."""
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
        return Score(score=55 if cat in ("transcription", "cloud_devops", "internship") else 30,
                     category=cat, reason="heuristic fallback (LLM unavailable)")

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
    return Score(score=score, category=category, reason=str(data.get("reason", ""))[:300], unpaid=unpaid)
