"""Marketplace deal-broker package (Phase 16) — the flash-flip pipeline.

Brokers underpriced, stale marketplace listings for resale margin with no inventory and no
capital risk: AgentOS never spends money (purchase requires an approved purchase gate) and
never messages a seller or buyer (it drafts; Calvin sends). Exposes SKILL for discovery.
"""

from skills.deal_broker.skill import SKILL

__all__ = ["SKILL"]
