"""Margin ledger tests (Phase 18).

The ledger RECORDS outcomes rather than tracking a speculative position: the seller price is
locked at NEGOTIATING and the buyer pays before PURCHASE_GATE, so margin is knowable before
any money moves. Losses of time (EXPIRED/REJECTED) are logged too — otherwise the weekly
report would only show wins and the hit rate would be a lie.
"""

from __future__ import annotations

import pytest

from tests.test_deal_broker import NOW, DAY, _FlipLLM, _seed_listing
from skills.deal_broker.skill import DealBrokerSkill


@pytest.fixture
def broker(mem, monkeypatch):
    import skills.deal_broker.skill as sk

    real = sk.get_settings()

    class _S:
        def __getattr__(self, n):
            return getattr(real, n)

        def get(self, *keys, default=None):
            if keys == ("flip", "resale_platforms"):
                return ["jiji"]
            if keys == ("flip", "platform_fee_pct"):
                return 0.05                      # 5% marketplace fee
            return real.get(*keys, default=default)

    monkeypatch.setattr(sk, "get_settings", lambda: _S())
    notes: list[str] = []
    skill = DealBrokerSkill(memory=mem, llm=_FlipLLM(), sources=[],
                            notify=lambda t: (notes.append(t), True)[1], clock=lambda: NOW)
    return skill, notes


def _list_flip(mem, skill, *, ext="a1", seller=18000.0, resale=25000.0):
    lid = _seed_listing(mem, ext=ext)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=seller)
    skill.list_item(listing_id=lid, resale_price=resale)
    return lid


def _close_flip(mem, skill, lid, *, paid=25000.0):
    skill.buyer_found(listing_id=lid, handle="b", platform="jiji", committed=True, paid=True,
                      amount=paid)
    skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    return lid


# ================================================================= open at LISTED
def test_position_opens_when_listed(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    pos = mem.get_position(lid)
    assert pos["status"] == "open"
    assert pos["seller_price"] == 18000 and pos["resale_price"] == 25000
    assert pos["flash_deadline"] == NOW + 48 * 3600
    assert pos["category"] == "phones"
    assert pos["buyer_paid_price"] is None       # nothing resolved yet


def test_no_position_before_listing(mem, broker):
    skill, _ = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    assert mem.get_position(lid) is None         # scoring rejects aren't "attempts"


# ================================================================= resolve at PURCHASE_GATE
def test_position_resolves_at_purchase_gate_with_fees(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    res = _close_flip(mem, skill, lid, paid=25000.0)
    pos = mem.get_position(lid)
    assert pos["status"] == "closed"
    assert pos["buyer_paid_price"] == 25000
    assert pos["fees"] == 1250.0                          # 5% of 25000
    assert pos["margin_abs"] == 25000 - 18000 - 1250      # = 5750
    assert pos["margin_pct"] == pytest.approx(5750 / 25000)   # margin on revenue
    assert pos["closed_at"] is not None


def test_gate_reports_the_margin(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    skill.buyer_found(listing_id=lid, handle="b", platform="jiji", committed=True, paid=True,
                      amount=25000)
    res = skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    assert res.data["margin_abs"] == 5750
    assert "Margin: 5750" in res.text


def test_blocked_gate_does_not_close_the_position(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    skill.buyer_found(listing_id=lid, handle="b", platform="jiji", committed=True, paid=True,
                      amount=25000)
    skill.purchase_gate(listing_id=lid, availability_confirmed=False, approve=True)   # blocked
    assert mem.get_position(lid)["status"] == "open"


# ================================================================= losses are logged too
def test_expired_flip_logs_a_zero_margin_attempt(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    for hours in (49, 200, 400):
        skill._now = lambda h=hours: NOW + h * 3600
        skill.expire_stale()
    pos = mem.get_position(lid)
    assert pos["status"] == "expired"
    assert pos["margin_abs"] == 0                # time spent, no margin — still an attempt


def test_rejected_flip_logs_a_zero_margin_attempt(mem, broker):
    skill, _ = broker
    lid = _list_flip(mem, skill)
    res = skill.reject_flip(listing_id=lid, reason="seller went silent")
    assert res.ok
    assert mem.get_position(lid)["status"] == "rejected"
    assert mem.active_platforms(lid) == []       # delisted everywhere on reject


# ================================================================= weekly report
def test_report_shows_true_hit_rate_not_just_wins(mem, broker):
    skill, _ = broker
    won = _list_flip(mem, skill, ext="w1")
    _close_flip(mem, skill, won, paid=25000)
    lost = _list_flip(mem, skill, ext="l1")
    skill.reject_flip(listing_id=lost, reason="seller pulled out")

    rep = skill.margin_report(days=7, notify=False)
    st = rep.data
    assert st["attempted"] == 2 and st["completed"] == 1 and st["rejected"] == 1
    assert st["hit_rate"] == 0.5                 # NOT 100% — losses count
    assert st["total_margin"] == 5750
    assert "Hit rate 50%" in rep.text


def test_report_ranks_best_and_worst_category(mem, broker):
    skill, _ = broker
    # phones: strong margin
    p = _list_flip(mem, skill, ext="p1", seller=10000, resale=25000)
    _close_flip(mem, skill, p, paid=25000)
    # laptops: thin margin
    mem.upsert_listing("jiji", "l9", title="Dell", make_model="dell l", condition="used",
                       category="laptops", asking_price=30000.0, listed_at=NOW - 30 * DAY)
    lid = mem.listing_by_ref("jiji", "l9")["id"]
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=29000)
    skill.list_item(listing_id=lid, resale_price=31000)
    _close_flip(mem, skill, lid, paid=31000)

    rep = skill.margin_report(days=7, notify=False)
    cats = rep.data["by_category"]
    assert cats[0]["category"] == "phones"       # best first
    assert cats[-1]["category"] == "laptops"
    assert "Best: phones" in rep.text


def test_empty_report_is_graceful(mem, broker):
    skill, _ = broker
    rep = skill.margin_report(days=7, notify=False)
    assert "no flips attempted" in rep.text


# ================================================================= persona feed
def test_report_feeds_verified_facts_to_persona(mem, broker, monkeypatch):
    from core.persona_store import PersonaEngine
    import skills.deal_broker.skill as sk

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(sk, "get_engine", lambda: engine)

    skill, _ = broker
    lid = _list_flip(mem, skill)
    _close_flip(mem, skill, lid, paid=25000)
    skill.margin_report(days=7, notify=False)

    facts = {f["key"]: f for f in engine.get_facts("flips")}
    assert "avg_margin_pct" in facts and "hit_rate" in facts and "best_category" in facts
    # measured from the ledger, so they're verified — Phase 16 can reason from them
    assert facts["avg_margin_pct"]["verified"] == 1
    assert facts["avg_margin_pct"]["source"] == "margin_ledger"
    assert "23%" in facts["avg_margin_pct"]["value"]      # 5750/25000


# ================================================================= standing instructions
def test_margin_floor_is_read_from_standing_instructions(mem, broker, monkeypatch):
    from core.persona_store import PersonaEngine
    import skills.deal_broker.skill as sk

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(sk, "get_engine", lambda: engine)
    skill, _ = broker

    assert skill._margin_floor() is None
    engine.remember("never pursue anything below a 25% margin")
    assert skill._margin_floor() == pytest.approx(0.25)


def test_floor_rejects_a_category_that_really_underperforms(mem, broker, monkeypatch):
    """The rule is checked against REAL ledger history, not a guess."""
    from core.persona_store import PersonaEngine
    import skills.deal_broker.skill as sk

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(sk, "get_engine", lambda: engine)
    skill, _ = broker
    engine.remember("never pursue anything below a 25% margin")

    # two real laptop flips that only returned ~6%
    for i in range(2):
        mem.upsert_listing("jiji", f"lap{i}", title="Dell", make_model="dell l", condition="used",
                           category="laptops", asking_price=30000.0, listed_at=NOW - 30 * DAY)
        lid = mem.listing_by_ref("jiji", f"lap{i}")["id"]
        mem.set_state(lid, "NEGOTIATING", "seed")
        skill.record_agreement(listing_id=lid, price=29000)
        skill.list_item(listing_id=lid, resale_price=31000)
        _close_flip(mem, skill, lid, paid=31000)

    observed = mem.category_margin("laptops")
    assert observed is not None and observed < 0.25
    ok, why = skill._passes_margin_floor("laptops", expected_margin_pct=0.40)
    assert ok is False
    assert "historically returns" in why and "25% floor" in why


def test_floor_rejects_a_thin_deal_even_without_history(mem, broker, monkeypatch):
    from core.persona_store import PersonaEngine
    import skills.deal_broker.skill as sk

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(sk, "get_engine", lambda: engine)
    skill, _ = broker
    engine.remember("never pursue anything below a 25% margin")

    ok, why = skill._passes_margin_floor("phones", expected_margin_pct=0.21)
    assert ok is False and "below your 25% floor" in why
    ok, _ = skill._passes_margin_floor("phones", expected_margin_pct=0.30)
    assert ok is True


def test_no_floor_means_no_extra_gate(mem, broker):
    skill, _ = broker
    ok, why = skill._passes_margin_floor("phones", expected_margin_pct=0.05)
    assert ok is True and why == ""
