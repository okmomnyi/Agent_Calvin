"""Transcription portal source (notify-only).

Rev, TranscribeMe, GoTranscript, Scribie, Verbit, Way With Words, Athreon and similar
require portal signup rather than an email application. Rather than scraping their
JS-heavy boards, we surface each as a notify-only entry (link + a ready application
blurb) exactly once, per the spec. The portal list lives in config.yaml.
"""

from __future__ import annotations

from core.config import get_settings
from core.logging_setup import get_logger
from skills.job_hunter.sources.base import RawJob, stable_id

log = get_logger("job_hunter.sources.portals")

# Fallback defaults if config.yaml doesn't define jobs.transcription_portals.
_DEFAULT_PORTALS = [
    {"name": "Rev", "url": "https://www.rev.com/freelancers"},
    {"name": "TranscribeMe", "url": "https://www.transcribeme.com/careers/"},
    {"name": "GoTranscript", "url": "https://gotranscript.com/transcription-jobs"},
    {"name": "Scribie", "url": "https://scribie.com/jobs"},
    {"name": "Verbit", "url": "https://verbit.ai/careers/"},
    {"name": "Way With Words", "url": "https://waywithwords.net/careers/"},
    {"name": "Athreon", "url": "https://www.athreon.com/careers/"},
]


class TranscriptionPortalsSource:
    name = "transcription_portals"
    enabled = True

    def fetch(self) -> list[RawJob]:
        portals = get_settings().get("jobs", "transcription_portals", default=None) or _DEFAULT_PORTALS
        jobs: list[RawJob] = []
        for p in portals:
            name = p.get("name", "")
            url = p.get("url", "")
            if not name or not url:
                continue
            jobs.append(RawJob(
                source="transcription_portals",
                external_id=stable_id("portal", name, url),
                title=f"{name} — transcription freelancer signup",
                company=name, url=url,
                description=p.get("note",
                    "Portal signup / application (no email apply). Register, complete the "
                    "grammar+transcription assessment, and start picking jobs."),
                category_hint="transcription", kind="notify_only",
            ))
        return jobs
