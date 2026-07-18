"""Marketplace deal-broker — the flash-flip pipeline (Phase 16).

Finds underpriced, stale (low-traction) listings and brokers them for resale margin without
ever holding inventory or capital risk.

    DISCOVERED -> SCORING -> NEGOTIATING -> LISTED -> BUYER_FOUND -> PURCHASE_GATE
                                                        -> PURCHASED -> DELIVERED
    (EXPIRED / REJECTED are terminal off-ramps from NEGOTIATING, LISTED, PURCHASE_GATE)

TWO HARD CAPITAL-RISK RULES, both structural rather than advisory:

1. **This skill cannot spend money.** There is no payment integration anywhere in it. The
   only path to PURCHASED is `confirm_purchase()`, which refuses unless an APPROVED
   purchase_gate_check exists — meaning a buyer has paid/firmly committed AND Calvin has
   re-confirmed the item is still available at the agreed price AND Calvin approved. That
   is what prevents ever taking a buyer's money for something that can't be delivered.

2. **This skill cannot message a seller or buyer.** It only drafts; Calvin sends, from his
   own account, in his own name. Automating a negotiating persona against strangers breaks
   platform ToS and hides from people whether they're talking to a human (§16 trust rules).

Notifications go to WhatsApp/SMS via Africa's Talking (not Telegram) — these are
time-sensitive and money-adjacent.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Sequence

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.persona_store import get_engine
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.whatsapp import send_whatsapp
from skills.deal_broker.scoring import config_from, score_listing
from skills.deal_broker.sources import RawListing, build_listing_sources

log = get_logger("skills.deal_broker")

# Legal state transitions — anything else is refused (and logged) rather than silently applied.
TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"SCORING", "REJECTED"},
    "SCORING": {"NEGOTIATING", "REJECTED"},
    "NEGOTIATING": {"LISTED", "REJECTED", "EXPIRED"},
    # EXPIRED/REJECTED are off-ramps from NEGOTIATING, LISTED and PURCHASE_GATE (§16):
    # a listed item can still be abandoned (seller pulls out, item not as described).
    "LISTED": {"BUYER_FOUND", "EXPIRED", "REJECTED"},
    "BUYER_FOUND": {"PURCHASE_GATE", "EXPIRED"},
    "PURCHASE_GATE": {"PURCHASED", "REJECTED", "EXPIRED"},
    "PURCHASED": {"DELIVERED"},
    "DELIVERED": set(),
    "EXPIRED": set(),
    "REJECTED": set(),
}


class DealBrokerSkill(BaseSkill):
    name = "deal_broker"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 sources: Sequence[Any] | None = None,
                 notify: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._llm = llm
        self._sources = list(sources) if sources is not None else None
        self._notify = notify or send_whatsapp
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def sources(self) -> list[Any]:
        if self._sources is None:
            self._sources = build_listing_sources()
        return self._sources

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "scan": self.scan, "pipeline": self.pipeline, "draft_negotiation": self.draft_negotiation,
            "record_agreement": self.record_agreement, "list_item": self.list_item,
            "buyer_found": self.buyer_found, "purchase_gate": self.purchase_gate,
            "confirm_purchase": self.confirm_purchase, "mark_delivered": self.mark_delivered,
            "expire_stale": self.expire_stale, "digest": self.digest,
            "inquiry": self.inquiry, "record_stats": self.record_stats,
            "ledger": self.ledger, "margin_report": self.margin_report,
            "reject_flip": self.reject_flip,
        }

    def contract(self) -> SkillContract:
        """No instruction can ever make it spend money or message a stranger itself."""
        return SkillContract(reads_categories=["flips", "notifications"],
                             hard_invariants=["never_spend_money",
                                              "never_message_counterparty",
                                              "never_buy_without_committed_buyer"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="flip.scan", func=self.scan, trigger="interval", kwargs={"hours": 6},
                         queued=True, skill="deal_broker", action="scan"),
            ScheduledJob(id="flip.expire", func=self.expire_stale, trigger="interval",
                         kwargs={"minutes": 30}),
            ScheduledJob(id="flip.digest", func=self.digest, trigger="cron", kwargs={"hour": 18}),
            # folds into the existing weekly-report cadence (Sunday evening)
            ScheduledJob(id="flip.margin_report", func=self.margin_report, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 18, "minute": 30}),
        ]

    # ------------------------------------------------------------- config
    @property
    def _cfg(self):
        return config_from(get_settings().get)

    def _window_hours(self) -> float:
        return float(get_settings().get("flip", "flash_window_hours", default=48))

    # ------------------------------------------------------------- state machine
    def _transition(self, listing_id: int, to_state: str, reason: str = "") -> bool:
        """Move a listing to a new state if the transition is legal. Never forces."""
        current = self.mem.get_state(listing_id)
        if current is None:
            if to_state != "DISCOVERED":
                log.warning("listing %s has no state; refusing %s", listing_id, to_state)
                return False
        elif to_state not in TRANSITIONS.get(current, set()):
            log.warning("illegal transition %s -> %s for listing %s", current, to_state, listing_id)
            return False
        self.mem.set_state(listing_id, to_state, reason)
        return True

    # ------------------------------------------------------------- scan + score
    def scan(self, **_: Any) -> CommandResult:
        """Fetch sources, dedupe, score. Keepers land in NEGOTIATING awaiting Calvin's message."""
        discovered = 0
        for source in self.sources:
            try:
                raws = source.fetch()
            except Exception:  # noqa: BLE001
                log.exception("flip source '%s' failed", getattr(source, "name", "?"))
                continue
            for raw in raws:
                if self._ingest(raw):
                    discovered += 1

        pursued = self._score_pending()
        return CommandResult(
            text=f"Scanned marketplaces — {discovered} new listing(s), {pursued} worth pursuing.",
            data={"discovered": discovered, "pursue": pursued})

    def _ingest(self, raw: RawListing) -> bool:
        is_new = self.mem.upsert_listing(
            raw.source, raw.external_id, url=raw.url, title=raw.title, category=raw.category,
            make_model=raw.make_model, condition=raw.condition, asking_price=raw.asking_price,
            currency=raw.currency, seller_ref=raw.seller_ref, listed_at=raw.listed_at,
            repost_count=raw.repost_count, price_drop_count=raw.price_drop_count,
            raw_json=json.dumps(raw.raw)[:8000])
        if is_new:
            row = self.mem.listing_by_ref(raw.source, raw.external_id)
            if row:
                self.mem.set_state(row["id"], "DISCOVERED", "scraped")
        return is_new

    def _score_pending(self) -> int:
        """Score everything in DISCOVERED. Pursue -> NEGOTIATING, else -> REJECTED."""
        pursued = 0
        for row in self.mem.listings_in_state("DISCOVERED"):
            self._transition(row["id"], "SCORING", "scoring")
            comp = self.mem.comp_median(row["make_model"] or "", row["condition"],
                                        exclude_id=row["id"])
            age_days = ((self._now() - row["listed_at"]) / 86400) if row["listed_at"] else 0.0
            s = score_listing(
                asking_price=row["asking_price"], comp_median=comp, listing_age_days=age_days,
                repost_count=row["repost_count"], price_drop_count=row["price_drop_count"],
                category_velocity_days=self._velocity(row["category"]), cfg=self._cfg)
            self.mem.save_score(
                row["id"], comp_median=s.comp_median, price_gap_pct=s.price_gap_pct,
                listing_age_days=s.listing_age_days, motivation=s.motivation,
                category_velocity_days=s.category_velocity_days, total=s.total,
                verdict=s.verdict, reason=s.reason)
            if s.verdict == "pursue":
                # Calvin's standing instructions get the final say, checked against REAL
                # ledger history where we have it (Phase 18) rather than a guess.
                ok, why = self._passes_margin_floor(row["category"], s.price_gap_pct)
                if not ok:
                    self._transition(row["id"], "REJECTED", why)
                    continue
                self._transition(row["id"], "NEGOTIATING", s.reason)
                pursued += 1
            else:
                self._transition(row["id"], "REJECTED", s.reason)
        return pursued

    def _velocity(self, category: str | None) -> float:
        """Time-to-sale for a category: REAL observed data once we have it, else the config guess.

        This is the Phase 17 analytics feedback loop — outcomes refine sourcing rather than
        sitting in a dashboard. Phase 18's margin ledger extends the same idea.
        """
        cat = (category or "").lower()
        observed = self.mem.observed_velocity(cat)
        if observed is not None:
            log.info("category '%s': using observed velocity %.1fd (beats config)", cat, observed)
            return observed
        table = get_settings().get("flip", "category_velocity_days", default={}) or {}
        return float(table.get(cat, table.get("default", 7)))

    # ------------------------------------------------------------- negotiation (DRAFT ONLY)
    def draft_negotiation(self, listing_id: int | str = 0, kind: str = "opening",
                          **_: Any) -> CommandResult:
        """Write a message for CALVIN to send. This skill never contacts the seller itself."""
        listing = self.mem.get_listing(int(listing_id))
        if not listing:
            return CommandResult(text=f"Listing {listing_id} not found.", ok=False)
        score = self.mem.get_score(int(listing_id)) or {}
        target = self._target_price(listing, score)
        try:
            draft = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "Write a short, friendly, professional marketplace message from a genuine "
                    "private buyer. Be respectful and human — no pressure tactics, no pretending "
                    "to be a company, no invented urgency. Plain text, 60 words max."},
                 {"role": "user", "content":
                    f"Item: {listing['title']} listed at {listing['currency']} "
                    f"{listing['asking_price']:.0f}. Comparable items sell around "
                    f"{score.get('comp_median') or 'unknown'}. I'd like to offer about "
                    f"{target:.0f} and ask if it's still available. Message type: {kind}."}],
                max_tokens=200).strip()
        except LLMError:
            draft = (f"Hi, is the {listing['title']} still available? "
                     f"Would you consider {listing['currency']} {target:.0f}? I can collect soon.")
        thread_id = self.mem.add_negotiation_draft(int(listing_id), kind, draft)
        return CommandResult(
            text=(f"✍️ Draft for you to send yourself (thread {thread_id}) — "
                  f"AgentOS does not message sellers:\n\n{draft}\n\n"
                  f"When you've sent it and agreed a price, run: record_agreement "
                  f"{listing_id} <price> --thread {thread_id}"),
            data={"thread_id": thread_id, "draft": draft, "target_price": target,
                  "sent_by_agent": False})

    def _target_price(self, listing: dict[str, Any], score: dict[str, Any]) -> float:
        comp = score.get("comp_median") or listing["asking_price"]
        floor = float(get_settings().get("flip", "opening_offer_pct", default=0.85))
        return round(min(listing["asking_price"], comp) * floor, 0)

    def record_agreement(self, listing_id: int | str = 0, price: float = 0.0,
                         thread_id: int | str = 0, **_: Any) -> CommandResult:
        """Calvin confirms HE sent the message and locked a seller price."""
        if not price:
            return CommandResult(text="Need the agreed seller price.", ok=False)
        if thread_id:
            self.mem.mark_negotiation_sent(int(thread_id), agreed_price=float(price))
        else:
            tid = self.mem.add_negotiation_draft(int(listing_id), "manual", "(sent by Calvin)")
            self.mem.mark_negotiation_sent(tid, agreed_price=float(price))
        return CommandResult(
            text=f"Seller price locked at {price:.0f} for listing {listing_id}. "
                 f"Next: list_item {listing_id} <resale_price>.",
            data={"agreed_price": float(price)})

    # ------------------------------------------------------------- listing (flash window opens)
    def _resale_platforms(self) -> list[str]:
        return list(get_settings().get("flip", "resale_platforms", default=["jiji"]) or ["jiji"])

    def list_item(self, listing_id: int | str = 0, resale_price: float = 0.0,
                  platform: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Cross-post the resale copy to every enabled marketplace at once (Phase 17).

        ONE flash window runs across all of them — not a separate window per platform. Still
        nothing bought: no capital is at risk until a buyer commits and the gate passes.
        """
        lid = int(listing_id)
        listing = self.mem.get_listing(lid)
        if not listing:
            return CommandResult(text=f"Listing {lid} not found.", ok=False)
        agreed = self.mem.agreed_price(lid)
        if agreed is None:
            return CommandResult(
                text="No agreed seller price yet — negotiate and run record_agreement first. "
                     "Listing before the seller price is locked would put you at risk.", ok=False)
        if not resale_price:
            score = self.mem.get_score(lid) or {}
            resale_price = round(float(score.get("comp_median") or listing["asking_price"]), 0)

        platforms = [platform] if platform else self._resale_platforms()
        copy_title, copy_body = self._resale_copy(listing, resale_price)   # one copy, reused
        deadline = self._now() + self._window_hours() * 3600               # one shared window
        for p in platforms:
            self.mem.add_resale_listing(lid, p, copy_title=copy_title, copy_body=copy_body,
                                        resale_price=float(resale_price), flash_deadline=deadline,
                                        posted_at=self._now())
        if self.mem.get_state(lid) != "LISTED":
            self._transition(lid, "LISTED", f"cross-posted at {resale_price:.0f} on {', '.join(platforms)}")
        # Phase 18: open the ledger position — seller price is already locked, so the margin
        # is knowable now; this records it rather than tracking a speculative position.
        self.mem.open_position(lid, category=listing["category"], seller_price=agreed,
                               resale_price=float(resale_price), flash_deadline=deadline,
                               now=self._now())
        msg = (f"🟢 LISTED on {len(platforms)} platform(s) ({', '.join(platforms)}): {listing['title']}\n"
               f"Buy at {agreed:.0f} → list at {resale_price:.0f} "
               f"(margin {resale_price - agreed:.0f})\n"
               f"One flash window closes in {self._window_hours():.0f}h across all of them.")
        if notify:
            self._notify(msg)
        return CommandResult(text=msg, data={"resale_price": float(resale_price),
                                             "agreed_price": agreed, "flash_deadline": deadline,
                                             "platforms": platforms})

    # ------------------------------------------------------------- buyer inquiries (DRAFT ONLY)
    def inquiry(self, listing_id: int | str = 0, platform: str = "", handle: str = "",
                message: str = "", **_: Any) -> CommandResult:
        """Draft a reply to an inbound buyer question. Same rule as negotiation: Calvin sends."""
        lid = int(listing_id)
        listing = self.mem.get_listing(lid)
        if not listing:
            return CommandResult(text=f"Listing {lid} not found.", ok=False)
        rows = self.mem.resale_listings(lid)
        price = rows[0]["resale_price"] if rows else None
        taken = self.mem.committed_buyer(lid) is not None
        try:
            draft = self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "Draft a short, friendly reply from a private seller to a buyer's question on a "
                    "marketplace. Be honest and concrete about availability, price stance, and "
                    "meetup/payment logistics. Never invent details about the item. 60 words max."},
                 {"role": "user", "content":
                    f"Item: {listing['title']}, listed at {price}. "
                    f"{'ALREADY COMMITTED to another buyer — say so honestly.' if taken else 'Still available.'}\n"
                    f"Buyer ({handle} on {platform}) asked: {message}"}],
                max_tokens=180).strip()
        except LLMError:
            draft = ("Yes, still available." if not taken else
                     "Sorry — it's just been committed to another buyer.")
        return CommandResult(
            text=f"✍️ Suggested reply to {handle} on {platform} — send it yourself:\n\n{draft}",
            data={"draft": draft, "sent_by_agent": False, "already_committed": taken})

    # ------------------------------------------------------------- analytics feedback (Phase 17)
    def record_stats(self, listing_id: int | str = 0, platform: str = "", views: int = 0,
                     inquiries: int = 0, **_: Any) -> CommandResult:
        """Record platform-reported views/inquiries — these refine category_velocity + scoring."""
        self.mem.record_platform_stats(int(listing_id), platform, views=int(views),
                                       inquiries=int(inquiries))
        return CommandResult(text=f"Recorded {views} view(s), {inquiries} inquiry(ies) for "
                                  f"listing {listing_id} on {platform}.",
                             data={"views": int(views), "inquiries": int(inquiries)})

    def _resale_copy(self, listing: dict[str, Any], price: float) -> tuple[str, str]:
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Write an honest resale listing for a used-goods marketplace. Describe only "
                    "what the source listing states — never invent condition, accessories, or "
                    "history. Return JSON."},
                 {"role": "user", "content":
                    f"Source listing: {listing['title']} ({listing['condition']}), "
                    f"category {listing['category']}. Asking {price:.0f} {listing['currency']}."}],
                schema_hint='{"title": string, "body": string}', max_tokens=400)
            return data.get("title", listing["title"]), data.get("body", "")
        except LLMError:
            return listing["title"], f"{listing['title']} — {listing['condition']}. Price {price:.0f}."

    # ------------------------------------------------------------- buyer
    def buyer_found(self, listing_id: int | str = 0, handle: str = "", platform: str = "jiji",
                    committed: bool = False, paid: bool = False, amount: float = 0.0,
                    notify: bool = True, **_: Any) -> CommandResult:
        """Record a buyer. Only a PAID/firmly-committed buyer advances the pipeline.

        FIRST-COMMITTED-BUYER WINS (Phase 17, hard rule — not configurable): the instant one
        platform reports a commitment, the item is delisted from every other platform. A
        later commitment on another platform is REFUSED — one item, one buyer. This is the
        same failure mode the purchase gate guards: never take money for something that's
        already gone.
        """
        lid = int(listing_id)
        if not self.mem.get_listing(lid):
            return CommandResult(text=f"Listing {lid} not found.", ok=False)

        existing = self.mem.committed_buyer(lid)
        if existing and (committed or paid):
            return CommandResult(
                text=(f"⛔ Refused: listing {lid} is already committed to "
                      f"{existing['handle']} on {existing['platform']} (first-committed-buyer "
                      f"wins). Do NOT take {handle}'s money — tell them it's gone."),
                ok=False, data={"blocked": "already_committed",
                                "winner": existing["handle"], "advanced": False})

        buyer_id = self.mem.add_buyer(lid, platform=platform, handle=handle,
                                      committed=bool(committed), paid=bool(paid),
                                      amount=float(amount) or None, now=self._now())
        if not (committed or paid):
            return CommandResult(
                text=f"Interest noted from {handle} (not a commitment) — item stays LISTED.",
                data={"buyer_id": buyer_id, "advanced": False})

        # Pull it from everywhere else FIRST, before anything else can happen.
        delisted = self.mem.delist_others(lid, keep_platform=platform)
        self._transition(lid, "BUYER_FOUND", f"{handle} committed on {platform}")
        self._transition(lid, "PURCHASE_GATE", "awaiting availability + approval")
        msg = (f"🔔 BUYER FOUND for listing {lid} ({handle}, {platform})"
               f"{f' — paid {amount:.0f}' if paid else ' — committed'}\n"
               f"🔒 Delisted from {delisted} other platform(s) immediately.\n"
               f"⛔ PURCHASE GATE: confirm the item is STILL AVAILABLE at the agreed price, "
               f"then approve. Nothing is bought until you do.")
        if notify:
            self._notify(msg)
        return CommandResult(text=msg, data={"buyer_id": buyer_id, "advanced": True,
                                             "delisted_elsewhere": delisted})

    # ------------------------------------------------------------- THE PURCHASE GATE
    def purchase_gate(self, listing_id: int | str = 0, availability_confirmed: bool = False,
                      approve: bool = False, **_: Any) -> CommandResult:
        """Evaluate the gate. Approves ONLY with a committed buyer + availability + Calvin's OK.

        Every evaluation is logged to purchase_gate_checks, pass or fail.
        """
        lid = int(listing_id)
        listing = self.mem.get_listing(lid)
        if not listing:
            return CommandResult(text=f"Listing {lid} not found.", ok=False)

        buyer = self.mem.committed_buyer(lid)
        buyer_committed = buyer is not None
        seller_price = self.mem.agreed_price(lid)

        blockers = []
        if not buyer_committed:
            blockers.append("no buyer has paid or firmly committed")
        if not availability_confirmed:
            blockers.append("item availability at the agreed price is not re-confirmed")
        if not approve:
            blockers.append("Calvin has not approved the purchase")
        if seller_price is None:
            blockers.append("no agreed seller price on file")

        decision = "approved" if not blockers else "blocked"
        reason = "all checks passed" if not blockers else "; ".join(blockers)
        self.mem.log_gate_check(
            lid, buyer_id=buyer["id"] if buyer else None, buyer_committed=buyer_committed,
            availability_confirmed=bool(availability_confirmed), calvin_approved=bool(approve),
            seller_price=seller_price, decision=decision, reason=reason)

        if decision == "blocked":
            return CommandResult(
                text=f"⛔ Purchase BLOCKED for listing {lid} — {reason}. Nothing was bought.",
                ok=False, data={"decision": decision, "blockers": blockers})
        # Phase 18: the position resolves here — buyer paid, seller price locked, fees known.
        paid = float(buyer.get("amount") or 0) if buyer else 0.0
        fees = round(paid * float(get_settings().get("flip", "platform_fee_pct", default=0.0)), 2)
        pos = self.mem.close_position(lid, buyer_paid_price=paid, fees=fees, now=self._now())
        margin_txt = (f" Margin: {pos['margin_abs']:.0f} ({pos['margin_pct']*100:.0f}%)."
                      if pos and pos["margin_abs"] is not None else "")
        return CommandResult(
            text=f"✅ Purchase gate passed for listing {lid} (buyer committed, availability "
                 f"confirmed, you approved).{margin_txt} Run confirm_purchase {lid} once you've "
                 f"paid the seller.",
            data={"decision": decision, "seller_price": seller_price,
                  "buyer_id": buyer["id"] if buyer else None,
                  "margin_abs": pos["margin_abs"] if pos else None,
                  "margin_pct": pos["margin_pct"] if pos else None})

    def confirm_purchase(self, listing_id: int | str = 0, **_: Any) -> CommandResult:
        """Record that Calvin bought the item. REFUSES without a passing gate check.

        AgentOS never spends money — Calvin pays; this only records the fact.
        """
        lid = int(listing_id)
        if not self.mem.has_passing_gate(lid):
            return CommandResult(
                text=f"⛔ Refusing to mark listing {lid} purchased — no approved purchase gate "
                     f"on record. Run purchase_gate first (needs a committed buyer, confirmed "
                     f"availability, and your approval).",
                ok=False, data={"blocked": "no_passing_gate"})
        if not self._transition(lid, "PURCHASED", "gate passed; Calvin paid the seller"):
            return CommandResult(text=f"Listing {lid} isn't at the purchase gate.", ok=False)
        return CommandResult(text=f"Recorded: listing {lid} purchased. Deliver to the buyer, "
                                  f"then run mark_delivered {lid}.", data={"state": "PURCHASED"})

    def mark_delivered(self, listing_id: int | str = 0, **_: Any) -> CommandResult:
        lid = int(listing_id)
        if not self._transition(lid, "DELIVERED", "handed to buyer"):
            return CommandResult(text=f"Listing {lid} isn't in PURCHASED state.", ok=False)
        return CommandResult(text=f"✅ Flip complete for listing {lid}.", data={"state": "DELIVERED"})

    # ------------------------------------------------------------- flash window expiry
    def expire_stale(self, notify: bool = True, **_: Any) -> CommandResult:
        """Flash window closed with no buyer anywhere -> drop a tier, or expire and delist.

        Price drops are applied UNIFORMLY across every active platform at once (Phase 17) —
        one item, one price, one window. Never per-platform.
        """
        now = self._now()
        tiers = get_settings().get("flip", "price_drop_tiers", default=[0.9, 0.8]) or []
        dropped = expired = 0
        for lid in self.mem.listings_with_expiring_windows(now):
            if self.mem.committed_buyer(lid):
                continue                       # a buyer exists; not a stall
            rows = self.mem.resale_listings(lid)
            if not rows:
                continue
            tier = min(r["tier"] for r in rows)          # the item's tier, not a platform's
            if tier < len(tiers):
                base = max(float(r["resale_price"]) for r in rows)
                new_price = round(base * float(tiers[tier]), 0)
                new_deadline = now + self._window_hours() * 3600
                for r in rows:                            # same price + window everywhere
                    self.mem.drop_resale_tier(r["id"], new_price, new_deadline)
                dropped += 1
                if notify:
                    self._notify(f"⬇️ No buyer in the window — dropping listing {lid} to "
                                 f"{new_price:.0f} across {len(rows)} platform(s) (tier {tier+1}).")
            else:
                for r in rows:
                    self.mem.set_resale_status(r["id"], "expired")
                self._transition(lid, "EXPIRED", "flash window closed, no buyer on any platform")
                self.mem.mark_position(lid, "expired", now=now)   # counts toward the true hit rate
                expired += 1
                if notify:
                    self._notify(f"⌛ EXPIRED: listing {lid} found no buyer on any platform — "
                                 f"delisted everywhere. Nothing was bought, no money at risk.")
        return CommandResult(text=f"Flash windows checked — {dropped} uniform price drop(s), "
                                  f"{expired} expired.",
                             data={"dropped": dropped, "expired": expired})

    # ------------------------------------------------------------- reject (logged as an attempt)
    def reject_flip(self, listing_id: int | str = 0, reason: str = "", **_: Any) -> CommandResult:
        """Abandon a flip (seller pulled out, item not as described, you changed your mind)."""
        lid = int(listing_id)
        if not self._transition(lid, "REJECTED", reason or "rejected by Calvin"):
            return CommandResult(text=f"Listing {lid} can't be rejected from its current state.",
                                 ok=False)
        for r in self.mem.resale_listings(lid):
            self.mem.set_resale_status(r["id"], "delisted")
        self.mem.mark_position(lid, "rejected", now=self._now())   # zero margin, still an attempt
        return CommandResult(text=f"Flip {lid} rejected ({reason or 'no reason given'}) and "
                                  f"delisted everywhere. Logged as an attempt — no margin.",
                             data={"state": "REJECTED"})

    # ------------------------------------------------------------- margin ledger (Phase 18)
    def ledger(self, listing_id: int | str = 0, **_: Any) -> CommandResult:
        """Show one flip's ledger position."""
        pos = self.mem.get_position(int(listing_id))
        if not pos:
            return CommandResult(text=f"No ledger position for flip {listing_id} "
                                      f"(positions open when an item is LISTED).", ok=False)
        line = (f"Flip {pos['flip_id']} [{pos['status']}] — buy {pos['seller_price'] or 0:.0f} → "
                f"list {pos['resale_price'] or 0:.0f}")
        if pos["status"] == "closed":
            line += (f" → paid {pos['buyer_paid_price']:.0f}, fees {pos['fees']:.0f}, "
                     f"margin {pos['margin_abs']:.0f} ({pos['margin_pct']*100:.0f}%)")
        return CommandResult(text=line, data=dict(pos))

    def margin_report(self, days: int = 7, notify: bool = True, **_: Any) -> CommandResult:
        """Weekly report: attempts vs. outcomes, margins, days-to-close, best/worst category.

        Also feeds the results back into the Persona KB so Phase 16 reasons from real numbers.
        """
        since = self._now() - days * 86400
        st = self.mem.ledger_stats(since)
        if not st["attempted"]:
            text = f"📒 Flip ledger ({days}d): no flips attempted."
            if notify:
                self._notify(text)
            return CommandResult(text=text, data=st)

        avg_pct = f"{st['avg_margin_pct']*100:.0f}%" if st["avg_margin_pct"] is not None else "n/a"
        avg_days = f"{st['avg_days_to_close']:.1f}d" if st["avg_days_to_close"] is not None else "n/a"
        lines = [
            f"📒 Flip ledger ({days}d)",
            f"Attempted {st['attempted']} · completed {st['completed']} · expired {st['expired']} "
            f"· rejected {st['rejected']} · open {st['open']}",
            f"Hit rate {st['hit_rate']*100:.0f}%   Total margin {st['total_margin']:.0f}   "
            f"Avg margin {avg_pct}   Avg days-to-close {avg_days}",
        ]
        if st["by_category"]:
            best = st["by_category"][0]
            worst = st["by_category"][-1]
            lines.append(f"Best: {best['category']} ({best['avg_margin_pct']*100:.0f}%, n={best['n']})"
                         + (f" · Worst: {worst['category']} ({worst['avg_margin_pct']*100:.0f}%, "
                            f"n={worst['n']})" if worst != best else ""))
        text = "\n".join(lines)
        self._feed_persona(st)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data=st)

    def _feed_persona(self, st: dict[str, Any]) -> None:
        """Turn real ledger outcomes into VERIFIED persona facts (measured, not guessed).

        Phase 16's negotiation drafts and scoring thresholds then reason from real numbers.
        """
        try:
            eng = get_engine()
            if st["avg_margin_pct"] is not None:
                eng.add_fact("flips", "avg_margin_pct",
                             f"{st['avg_margin_pct']*100:.0f}% average margin across "
                             f"{st['completed']} completed flip(s)",
                             source="margin_ledger", verified=True)
            eng.add_fact("flips", "hit_rate",
                         f"{st['completed']} of {st['attempted']} flip attempts completed "
                         f"({st['hit_rate']*100:.0f}%)", source="margin_ledger", verified=True)
            if st["by_category"]:
                b = st["by_category"][0]
                eng.add_fact("flips", "best_category",
                             f"{b['category']} at {b['avg_margin_pct']*100:.0f}% avg margin "
                             f"(n={b['n']})", source="margin_ledger", verified=True)
        except Exception:  # noqa: BLE001 - the report must not fail on a persona write
            log.exception("feeding ledger stats to the persona KB failed")

    # ------------------------------------------------------------- standing instructions
    _MARGIN_RE = __import__("re").compile(r"(\d+(?:\.\d+)?)\s*%\s*margin", __import__("re").I)

    def _margin_floor(self) -> float | None:
        """Read a margin floor out of Calvin's standing instructions, e.g.
        'never pursue anything below a 25% margin' -> 0.25."""
        try:
            rules = get_engine().relevant_instructions(["margin", "flip", "pursue"])
        except Exception:  # noqa: BLE001
            return None
        for rule in rules:
            m = self._MARGIN_RE.search(rule)
            if m:
                return float(m.group(1)) / 100.0
        return None

    def _passes_margin_floor(self, category: str | None, expected_margin_pct: float) -> tuple[bool, str]:
        """Check a deal against Calvin's margin floor using REAL ledger history where we have it."""
        floor = self._margin_floor()
        if floor is None:
            return True, ""
        observed = self.mem.category_margin((category or "").lower())
        if observed is not None and observed < floor:
            return False, (f"{category} historically returns {observed*100:.0f}% margin — below "
                           f"your {floor*100:.0f}% floor (from your standing instruction)")
        if expected_margin_pct < floor:
            return False, (f"expected margin {expected_margin_pct*100:.0f}% is below your "
                           f"{floor*100:.0f}% floor (standing instruction)")
        return True, ""

    # ------------------------------------------------------------- views
    def pipeline(self, **_: Any) -> CommandResult:
        rows = self.mem.execute(
            "SELECT state, COUNT(*) c FROM pipeline_state GROUP BY state").fetchall()
        counts = {r["state"]: r["c"] for r in rows}
        lines = [f"🔁 Flip pipeline: {counts or 'empty'}"]
        for r in self.mem.listings_in_state("PURCHASE_GATE"):
            lines.append(f"  ⛔ AWAITING YOUR APPROVAL: [{r['id']}] {r['title']}")
        for r in self.mem.listings_in_state("LISTED"):
            lines.append(f"  🟢 LISTED: [{r['id']}] {r['title']}")
        return CommandResult(text="\n".join(lines), data={"counts": counts})

    def digest(self, notify: bool = True, **_: Any) -> CommandResult:
        """Daily digest of everything LISTED and how long is left on each flash window."""
        now = self._now()
        listed = self.mem.listings_in_state("LISTED")
        if not listed:
            return CommandResult(text="No live flips right now.", data={"listed": 0})
        lines = ["📦 Live flips:"]
        for row in listed:
            for r in self.mem.resale_listings(row["id"]):
                left = (r["flash_deadline"] - now) / 3600
                lines.append(f"  [{row['id']}] {row['title']} @ {r['resale_price']:.0f} "
                             f"— {left:.0f}h left on {r['platform']}")
        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={"listed": len(listed)})


SKILL = DealBrokerSkill()
