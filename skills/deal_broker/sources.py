"""Marketplace listing sources for the flash-flip pipeline (Phase 16).

Sources are deliberately limited to lower-risk platforms with friendlier terms (Jiji,
Pigiame, OLX-style boards). Facebook Marketplace is NOT scraped: Meta actively pursues
scrapers, and the ban/legal exposure isn't worth it (§16 trust & compliance).

Parsers are pure functions over saved HTML/JSON so they're fixture-tested offline; the
network fetch reuses the polite job-hunter Fetcher (UA, >=2s/host, robots.txt, backoff).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.config import get_settings
from core.logging_setup import get_logger
from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import stable_id

log = get_logger("deal_broker.sources")

_PRICE_RE = re.compile(r"[\d][\d,\.]*")


@dataclass
class RawListing:
    """A normalized marketplace listing prior to scoring."""

    source: str
    external_id: str
    title: str
    url: str = ""
    category: str = ""
    make_model: str = ""
    condition: str = "used"
    asking_price: float | None = None
    currency: str = "KES"
    seller_ref: str = ""
    listed_at: float | None = None          # epoch of the seller's posting (staleness)
    repost_count: int = 0
    price_drop_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def age_days(self, now: float | None = None) -> float:
        if not self.listed_at:
            return 0.0
        return max(0.0, ((now or time.time()) - self.listed_at) / 86400)


def parse_price(text: str) -> float | None:
    """'KSh 12,500' -> 12500.0 . Returns None when no number is present."""
    m = _PRICE_RE.search((text or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def normalize_model(title: str) -> str:
    """Cheap make/model key for comps: lowercase alphanumerics, capacity/size kept."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    stop = {"for", "sale", "clean", "quick", "offer", "used", "new", "brand", "original",
            "cheap", "negotiable", "with", "and", "the", "in", "very", "good", "condition"}
    words = [w for w in t.split() if w not in stop and len(w) > 1]
    return " ".join(words[:4])


# ------------------------------------------------------------------ Jiji (JSON API)
def parse_jiji(data: Any, now: float | None = None) -> list[RawListing]:
    """Jiji's listing feed → RawListings. Tolerant of missing fields."""
    out: list[RawListing] = []
    for item in (data or {}).get("adverts_list", []) or []:
        title = item.get("title", "")
        price = item.get("price_obj", {}).get("value") if isinstance(item.get("price_obj"), dict) else None
        if price is None:
            price = parse_price(str(item.get("price", "")))
        listed = item.get("date_long") or item.get("created_at_long")
        out.append(RawListing(
            source="jiji", external_id=str(item.get("id") or stable_id("jiji", title)),
            title=title, url=item.get("url", ""), category=item.get("category_name", ""),
            make_model=normalize_model(title), condition=(item.get("condition") or "used").lower(),
            asking_price=float(price) if price is not None else None,
            seller_ref=str(item.get("user_id", "")),
            listed_at=float(listed) if listed else None,
            price_drop_count=int(item.get("price_changes", 0) or 0),
            raw=item))
    return out


# ------------------------------------------------------------------ Pigiame (HTML)
def parse_pigiame(html: str, now: float | None = None) -> list[RawListing]:
    """Pigiame-style listing cards → RawListings (pure; fixture-tested)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    out: list[RawListing] = []
    for card in soup.select(".listing-card"):
        a = card.select_one("a.listing-link")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url = a.get("href", "")
        price_el = card.select_one(".listing-price")
        age_el = card.select_one(".listing-age-days")
        listed_at = None
        if age_el:
            try:
                listed_at = (now or time.time()) - float(age_el.get_text(strip=True)) * 86400
            except ValueError:
                listed_at = None
        out.append(RawListing(
            source="pigiame", external_id=str(card.get("data-id") or stable_id("pigiame", url, title)),
            title=title, url=url,
            category=(card.get("data-category") or ""),
            make_model=normalize_model(title),
            condition=(card.get("data-condition") or "used").lower(),
            asking_price=parse_price(price_el.get_text(strip=True)) if price_el else None,
            seller_ref=(card.get("data-seller") or ""),
            listed_at=listed_at,
            repost_count=int(card.get("data-reposts") or 0),
            price_drop_count=int(card.get("data-price-drops") or 0),
            raw={"html_id": card.get("data-id")}))
    return out


class _Source:
    enabled = True

    def __init__(self, fetcher: Fetcher, name: str, url: str, parser, accept: str) -> None:
        self.fetcher, self.name, self.url, self._parser, self._accept = fetcher, name, url, parser, accept

    def fetch(self) -> list[RawListing]:
        resp = self.fetcher.get(self.url, accept=self._accept)
        if resp is None:
            return []
        try:
            payload = resp.json() if "json" in self._accept else resp.text
            return self._parser(payload)
        except Exception:  # noqa: BLE001 - one bad source never aborts a scan
            log.exception("%s: parse failed", self.name)
            return []


def build_listing_sources(fetcher: Fetcher | None = None) -> list[Any]:
    """Instantiate the enabled marketplace sources from config (flip.sources)."""
    fetcher = fetcher or Fetcher()
    cfg = get_settings().get("flip", "sources", default={}) or {}
    sources: list[Any] = []
    for name, spec in cfg.items():
        if not isinstance(spec, dict) or not spec.get("url") or not spec.get("enabled", True):
            continue
        if name == "jiji":
            sources.append(_Source(fetcher, "jiji", spec["url"], parse_jiji, "application/json"))
        elif name == "pigiame":
            sources.append(_Source(fetcher, "pigiame", spec["url"], parse_pigiame, "text/html"))
        else:
            log.warning("Unknown flip source '%s' in config — skipped", name)
    return sources
