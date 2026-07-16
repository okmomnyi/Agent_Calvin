"""Multi-platform distribution & buyer handling tests (Phase 17).

The headline rule is FIRST-COMMITTED-BUYER-WINS: the moment one platform reports a
commitment the item leaves every other platform, and a later commitment elsewhere is
refused. That's what stops a second buyer paying for something that no longer exists —
the same failure mode the Phase 16 purchase gate guards from the other side.
"""

from __future__ import annotations

import pytest

from tests.test_deal_broker import NOW, DAY, _FlipLLM, _seed_listing  # reuse fixtures/helpers
from skills.deal_broker.skill import DealBrokerSkill


@pytest.fixture
def broker(mem, monkeypatch):
    """Broker cross-posting to two platforms."""
    import skills.deal_broker.skill as sk

    real = sk.get_settings()

    class _S:
        def __getattr__(self, n):
            return getattr(real, n)

        def get(self, *keys, default=None):
            if keys == ("flip", "resale_platforms"):
                return ["jiji", "pigiame"]
            return real.get(*keys, default=default)

    monkeypatch.setattr(sk, "get_settings", lambda: _S())
    notes: list[str] = []
    skill = DealBrokerSkill(memory=mem, llm=_FlipLLM(), sources=[],
                            notify=lambda t: (notes.append(t), True)[1], clock=lambda: NOW)
    return skill, notes


def _listed(mem, skill, price=25000.0):
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    skill.list_item(listing_id=lid, resale_price=price)
    return lid


# ================================================================= cross-posting
def test_list_item_cross_posts_to_all_platforms(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    platforms = mem.active_platforms(lid)
    assert platforms == ["jiji", "pigiame"]


def test_cross_posts_share_one_flash_window_and_price(mem, broker):
    """One item, one price, one window — not a separate window per platform."""
    skill, _ = broker
    lid = _listed(mem, skill)
    rows = mem.resale_listings(lid)
    assert len({r["flash_deadline"] for r in rows}) == 1
    assert len({r["resale_price"] for r in rows}) == 1
    assert rows[0]["flash_deadline"] == NOW + 48 * 3600


def test_same_copy_reused_across_platforms(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    rows = mem.resale_listings(lid)
    assert len({r["copy_title"] for r in rows}) == 1     # generated once, posted everywhere


# ================================================================= FIRST-COMMITTED-BUYER WINS
def test_commitment_delists_every_other_platform_immediately(mem, broker):
    skill, notes = broker
    lid = _listed(mem, skill)
    assert len(mem.active_platforms(lid)) == 2

    res = skill.buyer_found(listing_id=lid, handle="wanjiku", platform="jiji",
                            committed=True, paid=True, amount=25000)
    assert res.data["delisted_elsewhere"] == 1
    assert mem.active_platforms(lid) == ["jiji"]          # only the winning platform remains
    assert any("Delisted from 1 other platform" in n for n in notes)


def test_second_buyer_on_another_platform_is_refused(mem, broker):
    """Never take a second buyer's money for an item that's already committed."""
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.buyer_found(listing_id=lid, handle="wanjiku", platform="jiji", committed=True, paid=True)

    res = skill.buyer_found(listing_id=lid, handle="otieno", platform="pigiame",
                            committed=True, paid=True, amount=25000)
    assert res.ok is False
    assert res.data["blocked"] == "already_committed"
    assert res.data["winner"] == "wanjiku"
    assert "Do NOT take otieno's money" in res.text
    # the losing buyer was never recorded as a commitment
    assert mem.committed_buyer(lid)["handle"] == "wanjiku"


def test_mere_interest_does_not_delist_anything(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.buyer_found(listing_id=lid, handle="browser", platform="jiji", committed=False)
    assert mem.active_platforms(lid) == ["jiji", "pigiame"]   # still live everywhere
    assert mem.get_state(lid) == "LISTED"


# ================================================================= uniform tier drops
def test_price_drop_is_uniform_across_platforms(mem, broker):
    skill, notes = broker
    lid = _listed(mem, skill)
    skill._now = lambda: NOW + 49 * 3600            # window closed, no buyer anywhere
    res = skill.expire_stale()
    assert res.data["dropped"] == 1                  # ONE drop for the item, not one per platform
    rows = mem.resale_listings(lid)
    assert {r["resale_price"] for r in rows} == {22500.0}      # same new price everywhere
    assert len({r["flash_deadline"] for r in rows}) == 1       # same new window everywhere
    assert all(r["tier"] == 1 for r in rows)
    assert any("across 2 platform(s)" in n for n in notes)


def test_expiry_delists_everywhere(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    for hours in (49, 200, 400):                     # tier 1, tier 2, then exhausted
        skill._now = lambda h=hours: NOW + h * 3600
        skill.expire_stale()
    assert mem.get_state(lid) == "EXPIRED"
    assert mem.active_platforms(lid) == []           # gone from every platform


def test_committed_item_is_never_price_dropped(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.buyer_found(listing_id=lid, handle="w", platform="jiji", committed=True, paid=True)
    skill._now = lambda: NOW + 49 * 3600
    res = skill.expire_stale()
    assert res.data["dropped"] == 0 and res.data["expired"] == 0


# ================================================================= inquiries (draft-only)
def test_inquiry_is_draft_only(mem, broker):
    skill, notes = broker
    lid = _listed(mem, skill)
    before = len(notes)                              # listing itself already alerted
    res = skill.inquiry(listing_id=lid, platform="pigiame", handle="asker",
                        message="is it still available?")
    assert res.data["sent_by_agent"] is False
    assert "send it yourself" in res.text
    assert len(notes) == before                      # drafting never sends or notifies


def test_inquiry_flags_already_committed_items(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.buyer_found(listing_id=lid, handle="wanjiku", platform="jiji", committed=True, paid=True)
    res = skill.inquiry(listing_id=lid, platform="pigiame", handle="latecomer", message="still there?")
    assert res.data["already_committed"] is True     # so the draft tells them honestly


# ================================================================= analytics feedback
def test_record_stats_persists_platform_traction(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.record_stats(listing_id=lid, platform="jiji", views=120, inquiries=4)
    row = [r for r in mem.resale_listings(lid) if r["platform"] == "jiji"][0]
    assert row["views"] == 120 and row["inquiries"] == 4


def test_observed_velocity_needs_enough_samples(mem, broker):
    skill, _ = broker
    lid = _listed(mem, skill)
    skill.buyer_found(listing_id=lid, handle="b", platform="jiji", committed=True, paid=True)
    assert mem.observed_velocity("phones") is None        # 1 sample < min 3


def test_observed_velocity_beats_config_once_learned(mem, broker):
    """Real outcomes feed back into sourcing rather than sitting in a dashboard."""
    skill, _ = broker
    for i in range(3):
        lid = _seed_listing(mem, ext=f"v{i}")
        mem.set_state(lid, "NEGOTIATING", "seed")
        skill.record_agreement(listing_id=lid, price=18000)
        skill.list_item(listing_id=lid, resale_price=25000)
        # each sells 2 days after it was posted (the skill's clock governs both timestamps)
        mem.add_buyer(lid, platform="jiji", handle=f"b{i}", committed=True, now=NOW + 2 * DAY)

    observed = mem.observed_velocity("phones")
    assert observed == pytest.approx(2.0, abs=0.1)
    assert skill._velocity("phones") == pytest.approx(2.0, abs=0.1)   # not the config's 3
