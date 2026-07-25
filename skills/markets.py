"""Markets awareness skill (Phase 38).

Real price data for crypto, forex, gold/oil, and major stock indices, shown alongside
concurrent business/world news headlines — NEVER a prediction, forecast, or buy/sell/hold
recommendation. This scope was confirmed explicitly (AskUserQuestion: "data + news
correlation only") after pushing back on the original ask's "predict markets" framing: no
model reliably predicts markets, and presenting a confident forecast would itself be a
fabrication (§0 P5 applies here as hard as anywhere in this codebase). This skill answers
"what moved, by how much, and what was in the news at the same time" — never "what will
happen" or "what you should do about it."

Data sources (free, keyless, verified live before wiring in):
  - CoinGecko `/simple/price` for crypto (BTC/ETH/SOL/... — 24h % change is built in).
  - Yahoo Finance's `/v8/finance/chart/{symbol}` for forex pairs (KES=X, EURUSD=X, ...),
    commodities (GC=F gold, CL=F crude oil), and stock indices (^GSPC, ...) — one request
    per symbol, since its batched `/v7/finance/quote` endpoint now returns "Unauthorized"
    without a session cookie/crumb the chart endpoint doesn't need. % change is computed
    from meta.regularMarketPrice vs meta.previousClose/chartPreviousClose, since the chart
    endpoint doesn't return a change figure directly.
  Stooq was tried first (per the original ask's spirit of using something well-known) and
  dropped: its public CSV quote endpoint (stooq.com/q/l/) currently returns a "page has
  moved" HTML response across every URL/domain/User-Agent variant tried, not CSV.

Robots.txt: CoinGecko's disallows /api/v3 (the exact endpoint used here), and Yahoo's
finance-quote host disallows everything. This was surfaced and confirmed explicitly before
writing any fetch code — it's a materially different case from world_news.py's RSS
respect_robots=False exception (an explicit path-level Disallow, not merely an absent
entry). Both are treated as documented/de-facto public JSON quote APIs meant to be polled
by tools (CoinGecko publishes this exact endpoint as its API; Yahoo's chart endpoint is
the one every finance library uses), not article/content pages — so respect_robots=False
is scoped ONLY to these two price endpoints, never to an article-body or HTML page fetch.

News correlation: reuses WorldNewsSkill.recent_headlines() (business + world categories)
rather than duplicating RSS-parsing logic — one set of feeds, one place that owns "what's
in the news right now." The single LLM call that writes the correlation commentary is
under the exact same never-invent discipline as world_news._synthesize: cite only
headlines actually fetched, hedge causation language ("amid reports of..."), and if
nothing in the fetched headlines plausibly explains a move, say only what moved — never
invent a reason, and never predict what happens next or suggest a trade. The rendered
snapshot also carries an explicit "not financial advice" line every time, not just in the
prompt — the constraint should be visible in the product, not only enforced upstream of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from skills.world_news import Headline, WorldNewsSkill

log = get_logger("skills.markets")


class Instrument(NamedTuple):
    name: str      # display name, e.g. "Bitcoin", "USD/KES", "Gold"
    symbol: str    # source-specific identifier, e.g. "bitcoin", "KES=X", "GC=F"
    source: str    # "coingecko" | "yahoo"


# The FALLBACK instrument list — production reads _instruments() live from config.yaml's
# markets.instruments every call (same convention world_news.py's _feeds() established),
# so adding/removing an instrument is a config edit, not a code change. This is what tests
# build fixtures against directly.
DEFAULT_INSTRUMENTS: dict[str, list[Instrument]] = {
    "crypto": [
        Instrument("Bitcoin", "bitcoin", "coingecko"),
        Instrument("Ethereum", "ethereum", "coingecko"),
        Instrument("Solana", "solana", "coingecko"),
    ],
    "forex": [
        # KES first — Kenya coverage matters throughout this codebase (world_news's own
        # kenya category), and open.er-api.com (the Frankfurter substitute found for
        # forex-only use) doesn't provide a % change, so Yahoo's KES=X ticker is used for
        # every forex pair here instead, keeping one consistent source with real % change.
        Instrument("USD/KES", "KES=X", "yahoo"),
        Instrument("USD/EUR", "EURUSD=X", "yahoo"),
        Instrument("USD/GBP", "GBPUSD=X", "yahoo"),
    ],
    "commodities": [
        Instrument("Gold", "GC=F", "yahoo"),
        Instrument("Crude Oil (WTI)", "CL=F", "yahoo"),
    ],
    "stocks": [
        Instrument("S&P 500", "^GSPC", "yahoo"),
        Instrument("Nasdaq Composite", "^IXIC", "yahoo"),
    ],
}


def _instruments() -> dict[str, list[Instrument]]:
    """The live instrument list (config-driven from the start — world_news.py only reached
    this pattern in a later slice, S6; no reason to relearn that lesson here). Falls back to
    DEFAULT_INSTRUMENTS if config.yaml's markets.instruments is missing or malformed."""
    raw = get_settings().get("markets", "instruments", default=None)
    if not raw:
        return DEFAULT_INSTRUMENTS
    try:
        return {
            category: [Instrument(i["name"], i["symbol"], i["source"]) for i in items]
            for category, items in raw.items()
        }
    except (KeyError, TypeError, AttributeError):
        log.warning("markets: config.yaml's markets.instruments is malformed, using defaults")
        return DEFAULT_INSTRUMENTS


CATEGORY_LABEL = {
    "crypto": "\U0001fa99 Crypto",
    "forex": "\U0001f4b1 Forex",
    "commodities": "\U0001f6e2 Commodities",
    "stocks": "\U0001f4c8 Stocks & Indices",
}

# News categories pulled for correlation context — business is the obvious fit, world for
# the "wars move oil/gold" case the original ask specifically named.
_NEWS_CATEGORIES_FOR_CORRELATION = ["business", "world"]
_MAX_HEADLINES_FOR_CONTEXT = 20  # keeps the LLM call small and bounded, not a full dump
# Only quotes moving at least this much (absolute %) get news-correlation commentary — a
# flat market doesn't need the model reaching for a headline to explain nothing.
_NOTABLE_MOVE_PCT_DEFAULT = 2.0


@dataclass
class Quote:
    category: str
    name: str
    symbol: str
    price: float
    change_pct: float | None  # None only if the source genuinely didn't provide one
    currency: str = "USD"


def _format_quote_line(q: Quote) -> str:
    change = "" if q.change_pct is None else f" ({q.change_pct:+.2f}%)"
    return f"{q.name}: {q.price:,.2f}{change}"


def _fetch_crypto(fetcher: Any, instruments: list[Instrument]) -> list[Quote]:
    """One batched CoinGecko call for every configured crypto id. Degrades to []  on any
    transport/parse failure — the caller treats a missing category the same as any other
    source outage (skip it this run, never fail the whole snapshot)."""
    if not instruments:
        return []
    ids = ",".join(i.symbol for i in instruments)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    resp = fetcher.get(url, accept="application/json")
    if resp is None or resp.status_code != 200:
        log.warning("markets: CoinGecko unreachable, skipping crypto this run")
        return []
    try:
        data = resp.json()
    except ValueError:
        log.warning("markets: CoinGecko returned unparseable JSON")
        return []
    out: list[Quote] = []
    for inst in instruments:
        row = data.get(inst.symbol)
        if not row or "usd" not in row:
            log.warning("markets: no CoinGecko data for %s, skipping", inst.name)
            continue
        out.append(Quote(category="crypto", name=inst.name, symbol=inst.symbol,
                          price=float(row["usd"]), change_pct=row.get("usd_24h_change")))
    return out


def _fetch_yahoo_one(fetcher: Any, inst: Instrument, category: str) -> Quote | None:
    """One symbol per call (Yahoo's batched quote endpoint now requires auth this project
    doesn't have — see module docstring). A single instrument failing degrades to None,
    never taking the rest of the snapshot down with it."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{inst.symbol}"
    resp = fetcher.get(url, accept="application/json")
    if resp is None or resp.status_code != 200:
        log.warning("markets: Yahoo unreachable for %s, skipping this run", inst.name)
        return None
    try:
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
    except (ValueError, KeyError, TypeError, IndexError):
        log.warning("markets: Yahoo returned an unexpected shape for %s, skipping", inst.name)
        return None
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    change_pct = ((price - prev) / prev * 100.0) if prev else None
    return Quote(category=category, name=inst.name, symbol=inst.symbol,
                 price=price, change_pct=change_pct)


class MarketsSkill(BaseSkill):
    name = "markets"

    def __init__(self, llm: LLMClient | None = None, fetcher: Any | None = None,
                 news_skill: WorldNewsSkill | None = None,
                 notify: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._llm = llm
        self._fetcher = fetcher
        self._news_skill = news_skill
        self._notify = notify or send_telegram
        self._now = clock

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def fetcher(self):
        if self._fetcher is None:
            from skills.job_hunter.fetcher import Fetcher

            # CoinGecko/Yahoo's price-quote JSON endpoints, not article/content pages — see
            # the module docstring's robots.txt section for why this is scoped narrowly here
            # and was confirmed explicitly rather than assumed from world_news's precedent.
            self._fetcher = Fetcher(respect_robots=False)
        return self._fetcher

    @property
    def news(self) -> WorldNewsSkill:
        if self._news_skill is None:
            # A fresh WorldNewsSkill with its OWN lazy fetcher/llm — deliberately not sharing
            # this skill's fetcher (different hosts, no benefit) or llm (keeps the two
            # skills' LLM call-sites independently testable/injectable).
            self._news_skill = WorldNewsSkill()
        return self._news_skill

    def contract(self) -> SkillContract:
        """`never_invents_a_source` mirrors world_news's own invariant (a correlation line
        cites a headline that was actually fetched, or it says nothing). `never_predicts_
        or_advises` is this skill's own non-negotiable: no forecast, no buy/sell/hold call,
        under any framing — this is the exact constraint the user confirmed the scope on."""
        return SkillContract(reads_categories=["tone", "general"],
                             hard_invariants=["never_invents_a_source", "never_predicts_or_advises"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"snapshot": self.snapshot, "markets": self.snapshot}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        hour = int(get_settings().get("markets", "briefing_hour", default=8))
        minute = int(get_settings().get("markets", "briefing_minute", default=0))
        return [ScheduledJob(id="markets.briefing", func=self.snapshot, trigger="cron",
                             kwargs={"hour": hour, "minute": minute})]

    # ------------------------------------------------------------- fetch
    def _fetch_all(self) -> list[Quote]:
        instruments = _instruments()
        quotes: list[Quote] = []
        try:
            quotes.extend(_fetch_crypto(self.fetcher, instruments.get("crypto", [])))
        except Exception:  # noqa: BLE001 - one category's failure must not block the rest
            log.warning("markets: crypto fetch failed", exc_info=True)
        for category in ("forex", "commodities", "stocks"):
            for inst in instruments.get(category, []):
                try:
                    q = _fetch_yahoo_one(self.fetcher, inst, category)
                except Exception:  # noqa: BLE001 - one instrument's failure must not block the rest
                    log.warning("markets: Yahoo fetch failed for %s", inst.name, exc_info=True)
                    q = None
                if q is not None:
                    quotes.append(q)
        return quotes

    # ------------------------------------------------------------- news correlation
    def _headlines_for_context(self) -> list[Headline]:
        out: list[Headline] = []
        for cat in _NEWS_CATEGORIES_FOR_CORRELATION:
            try:
                out.extend(self.news.recent_headlines(cat))
            except Exception:  # noqa: BLE001 - correlation context must never block the snapshot
                log.warning("markets: fetching %s headlines for correlation failed", cat, exc_info=True)
        return out[:_MAX_HEADLINES_FOR_CONTEXT]

    def _correlate(self, quotes: list[Quote], headlines: list[Headline]) -> str:
        """One LLM call, only when there's something to say: a notable mover AND at least
        one fetched headline to check against. Returns "" (no commentary section) rather
        than ever inventing a cause — the snapshot's raw numbers stand on their own."""
        threshold = float(get_settings().get(
            "markets", "notable_move_pct", default=_NOTABLE_MOVE_PCT_DEFAULT))
        movers = [q for q in quotes if q.change_pct is not None and abs(q.change_pct) >= threshold]
        if not movers or not headlines:
            return ""

        quote_lines = "\n".join(f"- {_format_quote_line(q)} [{q.category}]" for q in movers)
        headline_lines = "\n".join(f"- [{h.source}] {h.title}" for h in headlines)
        context = (f"PRICE MOVES (already computed, factual):\n{quote_lines}\n\n"
                   f"RECENT HEADLINES (already fetched, factual):\n{headline_lines}")
        try:
            answer = self.llm.chat(
                "research",
                [{"role": "system", "content":
                    "You annotate market price moves with news context. For each price "
                    "move listed, check the headlines for anything that plausibly relates "
                    "to it and write ONE short line, citing the headline's source by name "
                    "if you use it, e.g. '(Reuters)'. Use hedged language for any causal "
                    "link — 'amid reports of...', 'coincides with...' — never state a "
                    "cause as certain unless a headline says so explicitly. If no headline "
                    "relates to a move, write only what moved and by how much, with no "
                    "invented explanation. Do not predict what happens next, in any "
                    "timeframe. Do not give buy, sell, or hold advice, under any framing, "
                    "even hedged. Do not invent any fact, headline, or number not present "
                    "above."},
                 {"role": "user", "content": context}],
                max_tokens=350,
            )
        except LLMError:
            log.warning("markets: news-correlation synthesis failed, showing moves without commentary")
            return ""
        return answer.strip()

    # ------------------------------------------------------------- command
    def snapshot(self, notify: bool = True, **_: Any) -> CommandResult:
        quotes = self._fetch_all()
        stamp = time.strftime("%A %d %b %H:%M", time.localtime(self._now()))
        lines = [f"\U0001f4ca MARKETS SNAPSHOT · {stamp} UTC"]

        if not quotes:
            lines.append("(Couldn't reach any market data sources just now — try again shortly.)")
            text = "\n".join(lines)
            if notify:
                self._notify(text)
            return CommandResult(text=text, ok=False, data={"quote_count": 0})

        by_category: dict[str, list[Quote]] = {}
        for q in quotes:
            by_category.setdefault(q.category, []).append(q)

        for cat in ("crypto", "forex", "commodities", "stocks"):
            cat_quotes = by_category.get(cat)
            if not cat_quotes:
                continue
            lines.append(f"\n{CATEGORY_LABEL.get(cat, cat)}")
            lines.extend(f"  {_format_quote_line(q)}" for q in cat_quotes)

        headlines = self._headlines_for_context()
        commentary = self._correlate(quotes, headlines)
        if commentary:
            lines.append("\n\U0001f5de NEWS CONTEXT")
            lines.append(commentary)

        lines.append("\n(Data only — not financial advice. No predictions, no buy/sell calls.)")

        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, ok=True,
                             data={"quote_count": len(quotes),
                                   "categories": list(by_category.keys()),
                                   "has_commentary": bool(commentary)})


SKILL = MarketsSkill()
