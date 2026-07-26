"""Research skill package (Phase 39).

Web search + full-page fetch + cited synthesis, always delivered as a polished PDF
(never inline text — see skill.py's module docstring for why). Exposes SKILL for kernel
auto-discovery, plus ResearchSkill/DuckDuckGoSearcher for direct import (interview_prep.py
and study_vault.py both borrow this skill's search path for their own grounding).
"""

from skills.research.skill import SKILL, DuckDuckGoSearcher, ResearchSkill

__all__ = ["SKILL", "ResearchSkill", "DuckDuckGoSearcher"]
