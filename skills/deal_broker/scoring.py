"""Deal scoring for the flash-flip pipeline (Phase 16).

Pure and clock-injectable so it's fully unit-tested. Gates DISCOVERED -> NEGOTIATING.

BOTH sourcing filters must pass:
  * underpriced   — asking meaningfully below the comp median for that make/model/condition;
  * stale / low-traction — listing older than a threshold and/or seller-urgency signals
    (reposts, price drops). This is the real risk control: an unnoticed listing is far less
    likely to sell out from under Calvin mid-negotiation than a fresh, popular one. Most
    marketplaces don't expose view counts to non-owners, so traction is INFERRED from these
    proxies, never treated as a measured metric.

Plus the flash-sale constraint: categories whose historical time-to-sale exceeds the
configured ceiling are rejected at sourcing, so nothing that stalls ever enters the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoreConfig:
    min_price_gap_pct: float = 0.20      # >=20% below comp median
    min_listing_age_days: float = 14.0   # staleness proxy
    max_velocity_days: float = 7.0       # category must historically sell within this
    min_total: float = 60.0              # score needed to pursue


@dataclass
class DealScore:
    price_gap_pct: float
    listing_age_days: float
    motivation: int
    category_velocity_days: float
    total: float
    verdict: str          # pursue | reject
    reason: str
    comp_median: float | None = None


def score_listing(
    *,
    asking_price: float | None,
    comp_median: float | None,
    listing_age_days: float,
    repost_count: int = 0,
    price_drop_count: int = 0,
    category_velocity_days: float | None = None,
    cfg: ScoreConfig | None = None,
) -> DealScore:
    """Score a listing 0-100 and decide pursue/reject. Never guesses missing comps."""
    cfg = cfg or ScoreConfig()
    motivation = int(repost_count) + int(price_drop_count)
    velocity = category_velocity_days if category_velocity_days is not None else cfg.max_velocity_days

    def _reject(reason: str, gap: float = 0.0) -> DealScore:
        return DealScore(price_gap_pct=gap, listing_age_days=listing_age_days,
                         motivation=motivation, category_velocity_days=velocity,
                         total=0.0, verdict="reject", reason=reason, comp_median=comp_median)

    # No comp -> we cannot know it's underpriced. Never pursue on a guess.
    if not asking_price or not comp_median or comp_median <= 0:
        return _reject("no comparable median available — cannot establish it's underpriced")

    gap = (comp_median - asking_price) / comp_median

    # Flash-sale filter: slow categories never enter the pipeline.
    if velocity > cfg.max_velocity_days:
        return _reject(f"category too slow to flip ({velocity:.0f}d > {cfg.max_velocity_days:.0f}d)", gap)

    if gap < cfg.min_price_gap_pct:
        return _reject(f"only {gap*100:.0f}% below comp (need {cfg.min_price_gap_pct*100:.0f}%)", gap)

    # Staleness OR seller-motivation must be present — else it may sell mid-negotiation.
    stale = listing_age_days >= cfg.min_listing_age_days
    if not stale and motivation == 0:
        return _reject(
            f"listing is fresh ({listing_age_days:.0f}d) with no urgency signals — "
            "too likely to sell mid-negotiation", gap)

    # Everything above already passed the HARD filters (underpriced, not-fresh-or-motivated,
    # fast-enough category), so the deal is viable by definition. The score then ranks quality
    # above that floor: a base for clearing the gates, plus margin / staleness / urgency.
    def _clamp(x: float) -> float:
        return max(0.0, min(1.0, x))

    base = 40.0
    gap_pts = 35.0 * _clamp((gap - cfg.min_price_gap_pct) / max(1e-6, 0.5 - cfg.min_price_gap_pct))
    age_pts = 15.0 * _clamp(listing_age_days / 30.0)
    mot_pts = 10.0 * _clamp(motivation / 3.0)
    total = round(base + gap_pts + age_pts + mot_pts, 1)

    verdict = "pursue" if total >= cfg.min_total else "reject"
    reason = (f"{gap*100:.0f}% below comp, {listing_age_days:.0f}d old, "
              f"{motivation} urgency signal(s)")
    if verdict == "reject":
        reason += f" — score {total} below {cfg.min_total}"
    return DealScore(price_gap_pct=round(gap, 4), listing_age_days=listing_age_days,
                     motivation=motivation, category_velocity_days=velocity, total=total,
                     verdict=verdict, reason=reason, comp_median=comp_median)


def config_from(settings_get) -> ScoreConfig:
    """Build a ScoreConfig from config.yaml's flip section."""
    return ScoreConfig(
        min_price_gap_pct=float(settings_get("flip", "min_price_gap_pct", default=0.20)),
        min_listing_age_days=float(settings_get("flip", "min_listing_age_days", default=14)),
        max_velocity_days=float(settings_get("flip", "max_velocity_days", default=7)),
        min_total=float(settings_get("flip", "min_score", default=60)),
    )
