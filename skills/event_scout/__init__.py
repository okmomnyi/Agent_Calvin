"""Event Scout skill package (Phase 14).

Surfaces FREE events (online or physical) matching Calvin's interest tags — CTF
competitions, cloud/DevOps meetups, hackathons, etc. — deduped, ranked by tag match and
date proximity, with physical events biased to Nairobi/Mombasa. Exposes SKILL for discovery.
"""

from skills.event_scout.skill import SKILL

__all__ = ["SKILL"]
