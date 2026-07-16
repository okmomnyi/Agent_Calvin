"""Flash-flip pipeline tests (Phase 16).

The capital-risk rules are the whole point of this phase, so they get the most coverage:
  * AgentOS never spends money — purchase is impossible without an approved gate;
  * a buyer never pays against inventory that isn't locked in;
  * AgentOS never messages a seller — it drafts, Calvin sends;
  * nothing that stalls enters the pipeline, and stalled listings expire safely.
All network + LLM calls are mocked; the DB is the real Postgres test schema.
"""

from __future__ import annotations

import json

import pytest

from core.llm import LLMClient
from skills.deal_broker.scoring import ScoreConfig, score_listing
from skills.deal_broker.skill import TRANSITIONS, DealBrokerSkill
from skills.deal_broker.sources import RawListing, normalize_model, parse_jiji, parse_pigiame, parse_price

NOW = 1_800_000_000.0
DAY = 86400.0


# ================================================================= scoring (pure)
def test_underpriced_and_stale_is_pursued():
    s = score_listing(asking_price=7000, comp_median=10000, listing_age_days=20,
                      category_velocity_days=3)
    assert s.verdict == "pursue"
    assert s.price_gap_pct == pytest.approx(0.30)


def test_fresh_listing_without_urgency_is_rejected():
    """A fresh, unmotivated listing may sell mid-negotiation — that's the real risk control."""
    s = score_listing(asking_price=7000, comp_median=10000, listing_age_days=2,
                      category_velocity_days=3)
    assert s.verdict == "reject"
    assert "fresh" in s.reason


def test_fresh_but_motivated_seller_is_allowed():
    s = score_listing(asking_price=7000, comp_median=10000, listing_age_days=2,
                      repost_count=1, price_drop_count=2, category_velocity_days=3)
    assert s.verdict == "pursue"
    assert s.motivation == 3


def test_not_underpriced_enough_is_rejected():
    s = score_listing(asking_price=9500, comp_median=10000, listing_age_days=40,
                      category_velocity_days=3)
    assert s.verdict == "reject" and "below comp" in s.reason


def test_slow_category_never_enters_pipeline():
    """Flash-sale filter: slow-turnover categories are rejected at sourcing."""
    s = score_listing(asking_price=5000, comp_median=10000, listing_age_days=60,
                      category_velocity_days=45)          # e.g. vehicles
    assert s.verdict == "reject" and "too slow" in s.reason


def test_no_comp_never_guesses():
    s = score_listing(asking_price=5000, comp_median=None, listing_age_days=60)
    assert s.verdict == "reject" and "no comparable" in s.reason


# ================================================================= parsers
def test_parse_price_and_model():
    assert parse_price("KSh 12,500") == 12500.0
    assert parse_price("no digits") is None
    assert normalize_model("Clean Used iPhone 11 64GB for sale") == "iphone 11 64gb"


def test_parse_jiji():
    data = {"adverts_list": [{"id": 7, "title": "iPhone 11 64GB", "price_obj": {"value": 22000},
                              "url": "https://jiji/7", "category_name": "phones",
                              "user_id": 42, "date_long": NOW - 20 * DAY, "price_changes": 2}]}
    out = parse_jiji(data)
    assert out[0].asking_price == 22000 and out[0].price_drop_count == 2
    assert out[0].make_model == "iphone 11 64gb"


def test_parse_pigiame():
    html = """<div class="listing-card" data-id="p1" data-category="laptops"
                   data-condition="used" data-reposts="1" data-price-drops="1">
                <a class="listing-link" href="/l/p1">Dell Latitude 7490 i5</a>
                <span class="listing-price">KSh 32,000</span>
                <span class="listing-age-days">18</span>
              </div>"""
    out = parse_pigiame(html, now=NOW)
    assert out[0].asking_price == 32000.0
    assert out[0].repost_count == 1
    assert out[0].age_days(NOW) == pytest.approx(18, abs=0.1)


# ================================================================= skill fixture
class _FlipLLM(LLMClient):
    def __init__(self):
        self.routes = {"default": "m", "write": "m"}
        self.defaults = {}

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "Hi, is this still available? Would you take 18,000? I can collect today."

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return {"title": "iPhone 11 64GB — clean", "body": "Used, as described by the seller."}


class _FakeSource:
    name = "fake"
    enabled = True

    def __init__(self, listings):
        self._l = listings

    def fetch(self):
        return self._l


@pytest.fixture
def broker(mem):
    notes: list[str] = []
    skill = DealBrokerSkill(memory=mem, llm=_FlipLLM(), sources=[],
                            notify=lambda t: (notes.append(t), True)[1], clock=lambda: NOW)
    return skill, notes


def _seed_listing(mem, *, asking=18000.0, age_days=30.0, model="iphone 11 64gb",
                  category="phones", ext="a1"):
    mem.upsert_listing("jiji", ext, title="iPhone 11 64GB", url="u", category=category,
                       make_model=model, condition="used", asking_price=asking,
                       listed_at=NOW - age_days * DAY)
    return mem.listing_by_ref("jiji", ext)["id"]


def _comparables(mem, model="iphone 11 64gb", category="phones"):
    # three comps at ~25-26k so the median makes 18k clearly underpriced
    for i, price in enumerate([25000.0, 26000.0, 25500.0]):
        mem.upsert_listing("jiji", f"comp{i}", title="iPhone 11 64GB", make_model=model,
                           condition="used", category=category, asking_price=price,
                           listed_at=NOW - 5 * DAY)


# ================================================================= scan → score → state
def test_scan_discovers_scores_and_pursues(mem, broker):
    skill, _ = broker
    _comparables(mem)
    raw = RawListing(source="jiji", external_id="deal1", title="iPhone 11 64GB",
                     category="phones", make_model="iphone 11 64gb", condition="used",
                     asking_price=18000.0, listed_at=NOW - 30 * DAY)
    skill._sources = [_FakeSource([raw])]
    res = skill.scan()
    assert res.data["discovered"] == 1 and res.data["pursue"] == 1
    lid = mem.listing_by_ref("jiji", "deal1")["id"]
    assert mem.get_state(lid) == "NEGOTIATING"
    assert mem.get_score(lid)["verdict"] == "pursue"


def test_scan_rejects_slow_category(mem, broker):
    skill, _ = broker
    for i, p in enumerate([90000.0, 100000.0]):
        mem.upsert_listing("jiji", f"sofa{i}", title="Sofa set", make_model="sofa set",
                           condition="used", category="furniture", asking_price=p,
                           listed_at=NOW - 5 * DAY)
    raw = RawListing(source="jiji", external_id="sofa9", title="Sofa set", category="furniture",
                     make_model="sofa set", condition="used", asking_price=50000.0,
                     listed_at=NOW - 60 * DAY)
    skill._sources = [_FakeSource([raw])]
    skill.scan()
    lid = mem.listing_by_ref("jiji", "sofa9")["id"]
    assert mem.get_state(lid) == "REJECTED"       # furniture velocity 21d > 7d ceiling


def test_scan_is_idempotent(mem, broker):
    skill, _ = broker
    raw = RawListing(source="jiji", external_id="dup", title="X", make_model="x",
                     asking_price=100.0, listed_at=NOW - 30 * DAY)
    skill._sources = [_FakeSource([raw])]
    assert skill.scan().data["discovered"] == 1
    assert skill.scan().data["discovered"] == 0


# ================================================================= negotiation is DRAFT-ONLY
def test_negotiation_is_draft_only_never_sent(mem, broker):
    skill, notes = broker
    _comparables(mem)
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    res = skill.draft_negotiation(listing_id=lid)
    assert res.data["sent_by_agent"] is False
    assert "does not message sellers" in res.text
    # stored as a draft with NO sent timestamp — only Calvin can set that
    row = mem.execute("SELECT * FROM negotiation_threads WHERE listing_id=%s", (lid,)).fetchone()
    assert row["sent_by_calvin_at"] is None
    assert notes == []                       # drafting never notifies/sends anything


def test_skill_has_no_seller_messaging_api():
    """Structural: there is no method that could contact a seller/buyer."""
    api = set(DealBrokerSkill(memory=None).commands())
    assert not {"send", "send_message", "reply", "message_seller"} & api


# ================================================================= listing / flash window
def test_cannot_list_before_seller_price_is_locked(mem, broker):
    skill, _ = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    res = skill.list_item(listing_id=lid, resale_price=25000)
    assert res.ok is False
    assert "record_agreement first" in res.text
    assert mem.get_state(lid) == "NEGOTIATING"     # never advanced


def test_list_item_opens_flash_window(mem, broker):
    skill, notes = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    res = skill.list_item(listing_id=lid, resale_price=25000)
    assert res.ok and mem.get_state(lid) == "LISTED"
    assert res.data["flash_deadline"] == NOW + 48 * 3600
    assert any("LISTED" in n for n in notes)       # WhatsApp notified


def test_flash_window_drops_tier_then_expires(mem, broker):
    skill, notes = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    skill.list_item(listing_id=lid, resale_price=25000)

    skill._now = lambda: NOW + 49 * 3600          # window closed, no buyer
    first = skill.expire_stale()
    assert first.data["dropped"] == 1             # tier 1: 25000 -> 22500
    assert mem.resale_listings(lid)[0]["resale_price"] == 22500

    skill._now = lambda: NOW + 200 * 3600
    skill.expire_stale()                          # tier 2 -> 18000
    skill._now = lambda: NOW + 400 * 3600
    last = skill.expire_stale()                   # tiers exhausted -> expire
    assert last.data["expired"] == 1
    assert mem.get_state(lid) == "EXPIRED"
    assert any("Nothing was bought" in n for n in notes)


# ================================================================= THE PURCHASE GATE
def _to_gate(mem, skill, *, paid=True):
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    skill.list_item(listing_id=lid, resale_price=25000)
    skill.buyer_found(listing_id=lid, handle="buyer1", committed=True, paid=paid, amount=25000)
    return lid


def test_mere_interest_does_not_advance_pipeline(mem, broker):
    skill, _ = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    skill.list_item(listing_id=lid, resale_price=25000)
    res = skill.buyer_found(listing_id=lid, handle="tyre_kicker", committed=False, paid=False)
    assert res.data["advanced"] is False
    assert mem.get_state(lid) == "LISTED"          # still just listed


def test_committed_buyer_moves_to_purchase_gate(mem, broker):
    skill, notes = broker
    lid = _to_gate(mem, skill)
    assert mem.get_state(lid) == "PURCHASE_GATE"
    assert any("PURCHASE GATE" in n for n in notes)


def test_gate_blocks_without_availability_confirmation(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    res = skill.purchase_gate(listing_id=lid, availability_confirmed=False, approve=True)
    assert res.ok is False
    assert "availability" in res.text
    assert mem.has_passing_gate(lid) is False


def test_gate_blocks_without_calvins_approval(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    res = skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=False)
    assert res.ok is False
    assert "not approved" in res.text
    assert mem.has_passing_gate(lid) is False


def test_gate_blocks_without_committed_buyer(mem, broker):
    """Never buy on spec — a buyer must have paid/committed first."""
    skill, _ = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "NEGOTIATING", "seed")
    skill.record_agreement(listing_id=lid, price=18000)
    skill.list_item(listing_id=lid, resale_price=25000)
    res = skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    assert res.ok is False
    assert "no buyer" in res.text
    assert mem.has_passing_gate(lid) is False


def test_gate_passes_only_with_all_three(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    res = skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    assert res.ok and res.data["decision"] == "approved"
    assert mem.has_passing_gate(lid) is True


def test_every_gate_evaluation_is_audited(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    skill.purchase_gate(listing_id=lid, availability_confirmed=False, approve=True)
    skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    checks = mem.gate_checks(lid)
    assert [c["decision"] for c in checks] == ["blocked", "approved"]


# ================================================================= NEVER SPEND MONEY
def test_confirm_purchase_refuses_without_passing_gate(mem, broker):
    """The core capital-risk rule: no purchase can be recorded without an approved gate."""
    skill, _ = broker
    lid = _to_gate(mem, skill)
    res = skill.confirm_purchase(listing_id=lid)
    assert res.ok is False
    assert res.data["blocked"] == "no_passing_gate"
    assert mem.get_state(lid) == "PURCHASE_GATE"       # never advanced to PURCHASED


def test_confirm_purchase_refuses_after_a_blocked_gate(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    skill.purchase_gate(listing_id=lid, availability_confirmed=False, approve=True)  # blocked
    assert skill.confirm_purchase(listing_id=lid).ok is False
    assert mem.get_state(lid) != "PURCHASED"


def test_full_happy_path_to_delivered(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    skill.purchase_gate(listing_id=lid, availability_confirmed=True, approve=True)
    assert skill.confirm_purchase(listing_id=lid).ok
    assert mem.get_state(lid) == "PURCHASED"
    assert skill.mark_delivered(listing_id=lid).ok
    assert mem.get_state(lid) == "DELIVERED"


# ================================================================= state machine
def test_illegal_transitions_are_refused(mem, broker):
    skill, _ = broker
    lid = _seed_listing(mem)
    mem.set_state(lid, "DISCOVERED", "seed")
    assert skill._transition(lid, "PURCHASED", "cheating") is False   # skips the whole pipeline
    assert mem.get_state(lid) == "DISCOVERED"


def test_terminal_states_are_dead_ends():
    assert TRANSITIONS["EXPIRED"] == set()
    assert TRANSITIONS["REJECTED"] == set()
    assert TRANSITIONS["DELIVERED"] == set()


def test_transitions_are_logged_immutably(mem, broker):
    skill, _ = broker
    lid = _to_gate(mem, skill)
    states = [t["to_state"] for t in mem.transitions(lid)]
    assert states[:1] == ["NEGOTIATING"]
    assert "LISTED" in states and "BUYER_FOUND" in states and "PURCHASE_GATE" in states
