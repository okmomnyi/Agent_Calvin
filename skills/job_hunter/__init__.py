"""Job hunter skill package (Phase 3).

Scrapes a modular registry of job sources, dedupes and category-scores each posting,
drafts a cover email from the verified persona KB (never inventing experience), and
tracks applications. Exposes SKILL for kernel auto-discovery.
"""

from skills.job_hunter.skill import SKILL

__all__ = ["SKILL"]
