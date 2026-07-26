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

Fallback keys (added after the two free primaries proved unreliable in exactly one way
each — Yahoo goes empty when a market's session is closed, see the closed-market fallback
below; CoinGecko's public endpoint has no key and no rate-limit guarantee at all):
  - CoinMarketCap `/v1/cryptocurrency/quotes/latest` — crypto only, tried ONLY when
    CoinGecko itself returns nothing. Requires `COINMARKETCAP_API_KEY`; a bare-symbol
    alias table (`markets.coinmarketcap_symbols` in config.yaml) bridges CoinGecko's
    lowercase ids to CMC's ticker symbols for the configured instruments.
  - Alpha Vantage `CURRENCY_EXCHANGE_RATE` — forex only, tried ONLY when Yahoo's forex
    quote fails. Requires `ALPHA_VANTAGE_API_KEY`. Deliberately NOT extended to
    commodities or stock indices: Alpha Vantage has no gold-spot function at all, and
    approximating an INDEX via a tracking ETF (SPY for the S&P 500, say) would silently
    substitute a different, related-but-not-identical number under the same label — the
    exact class of quiet inaccuracy this codebase exists to avoid (§0 P5). Alpha Vantage's
    free tier is also severely rate-limited (historically ~25 requests/day), which a
    blanket fallback across every instrument would burn through in a single briefing.
  Neither key touches the CHART series path (`_fetch_*_chart_series`) — only the
  point-in-time quote. The closed-market chart fallback (wider Yahoo daily-bar windows)
  already covers the observed gap there without needing a second provider.

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

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.stage import ChartWidget, TickerWidget, Widget
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

# Phase 38 stage widget: source -> the honesty label that must ride on every chart widget
# from that source (§0 P5 applies to the product surface, not only to what the model is
# told). CoinGecko's simple/price and market_chart are effectively real-time free data;
# Yahoo's free chart endpoint is the industry-standard ~15-minute-delayed quote.
_DELAYED_LABEL = {"coingecko": "real-time", "yahoo": "~15m delayed (free feed)"}

# category -> the Widget.klass vocabulary the frontend/prototype spec defines. A category
# with no mapping here (a future config addition) falls back to its own name unchanged.
_KLASS_FOR_CATEGORY = {"crypto": "crypto", "forex": "fx", "commodities": "commodity",
                       "stocks": "equity", "nse": "nse"}

# Small alias table so a spoken/typed asset name resolves without a new taxonomy ("show
# oil" -> the configured "Crude Oil (WTI)" instrument). Deliberately literal, not
# fuzzy/LLM matching, so asset resolution stays fast and deterministic — display() can run
# on every stage subject change. Falls through to a plain substring match against whatever
# is actually configured (so a custom config.yaml instrument is still reachable by name).
_ASSET_ALIASES: dict[str, tuple[str, str]] = {
    "btc": ("crypto", "Bitcoin"), "bitcoin": ("crypto", "Bitcoin"),
    "eth": ("crypto", "Ethereum"), "ethereum": ("crypto", "Ethereum"),
    "sol": ("crypto", "Solana"), "solana": ("crypto", "Solana"),
    "oil": ("commodities", "Crude Oil (WTI)"), "crude": ("commodities", "Crude Oil (WTI)"),
    "wti": ("commodities", "Crude Oil (WTI)"),
    "gold": ("commodities", "Gold"), "xau": ("commodities", "Gold"),
    "kes": ("forex", "USD/KES"), "shilling": ("forex", "USD/KES"),
    "sp500": ("stocks", "S&P 500"), "s&p": ("stocks", "S&P 500"),
    "nasdaq": ("stocks", "Nasdaq Composite"),
}

# Chart range -> CoinGecko's `days` query param.
_CRYPTO_RANGE_DAYS = {"1d": 1, "1w": 7, "1m": 30}
# Chart range -> Yahoo's (range, interval) query params.
_YAHOO_RANGE_PARAMS = {"1d": ("1d", "5m"), "1w": ("5d", "30m"), "1m": ("1mo", "1d")}


def _resolve_asset(hint: str, instruments: dict[str, list[Instrument]]
                   ) -> tuple[Instrument | None, str | None]:
    """A spoken/typed asset name -> (Instrument, category), or (None, None) if it can't be
    resolved. The caller must omit the chart widget rather than guess."""
    h = (hint or "").strip().lower()
    if not h:
        return None, None
    for alias, (cat_key, name_key) in _ASSET_ALIASES.items():
        if alias in h:
            for inst in instruments.get(cat_key, []):
                if inst.name == name_key:
                    return inst, cat_key
    for category, insts in instruments.items():
        for inst in insts:
            name_lower = inst.name.lower()
            if name_lower in h or h in name_lower:
                return inst, category
    return None, None


def _fetch_crypto_chart_series(fetcher: Any, coingecko_id: str,
                               range_key: str) -> list[dict[str, float]]:
    """CoinGecko's own `/market_chart` endpoint — same host, same robots.txt exception
    already justified in this module's docstring for `/simple/price`. Degrades to [] (no
    chart, never a fabricated series) on any transport/parse failure."""
    days = _CRYPTO_RANGE_DAYS.get(range_key, 1)
    url = f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart?vs_currency=usd&days={days}"
    resp = fetcher.get(url, accept="application/json")
    if resp is None or resp.status_code != 200:
        log.warning("markets: CoinGecko market_chart unreachable for %s", coingecko_id)
        return []
    try:
        prices = resp.json()["prices"]
    except (ValueError, KeyError, TypeError):
        log.warning("markets: CoinGecko market_chart returned an unexpected shape for %s",
                   coingecko_id)
        return []
    return [{"t": p[0] / 1000.0, "v": float(p[1])} for p in prices
            if isinstance(p, list) and len(p) == 2 and p[1] is not None]


# Widened fallback windows, daily bars only -- tried in order when the requested
# (range, interval) comes back empty. Verified live against the real endpoint (2026-07-26):
# when a commodity/stock's exchange session is closed (after-hours, weekend), Yahoo's
# intraday chart returns NO "timestamp" key at all and an empty indicators.quote — not a
# malformed response, just nothing to show for THAT window. A daily-bar window is real
# data almost always present regardless of whether the market happens to be open right
# now, so this still satisfies "never synthesize a tick" -- every point is still a real
# close Yahoo reported, just from a wider, coarser window than first requested.
_YAHOO_FALLBACK_PARAMS: list[tuple[str, str]] = [("5d", "1d"), ("1mo", "1d")]


def _fetch_yahoo_chart_series_once(fetcher: Any, symbol: str, rng: str,
                                   interval: str) -> list[dict[str, float]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval={interval}"
    resp = fetcher.get(url, accept="application/json")
    if resp is None or resp.status_code != 200:
        log.warning("markets: Yahoo chart series unreachable for %s", symbol)
        return []
    try:
        result = resp.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (ValueError, KeyError, TypeError, IndexError):
        log.warning("markets: Yahoo chart series returned an unexpected shape for %s "
                   "(range=%s interval=%s) — likely no session data for this window",
                   symbol, rng, interval)
        return []
    return [{"t": float(t), "v": float(c)} for t, c in zip(timestamps, closes) if c is not None]


def _fetch_yahoo_chart_series(fetcher: Any, symbol: str, range_key: str) -> list[dict[str, float]]:
    """The SAME Yahoo chart endpoint `_fetch_yahoo_one` already calls, just asking for the
    series (range/interval) it always returns instead of reading only `.meta`. Null closes
    (a gap in Yahoo's own intraday data) are dropped rather than interpolated — never
    synthesize a tick that didn't come from the source.

    Falls back to progressively wider daily-bar windows when the requested window is
    genuinely empty (the exchange session is closed right now) — real ticks from a wider
    window beat no chart at all for an asset that trades on a schedule, and every point
    returned is still exactly what Yahoo reported, never invented.
    """
    rng, interval = _YAHOO_RANGE_PARAMS.get(range_key, _YAHOO_RANGE_PARAMS["1d"])
    series = _fetch_yahoo_chart_series_once(fetcher, symbol, rng, interval)
    if series:
        return series
    for fallback_rng, fallback_interval in _YAHOO_FALLBACK_PARAMS:
        if (fallback_rng, fallback_interval) == (rng, interval):
            continue
        series = _fetch_yahoo_chart_series_once(fetcher, symbol, fallback_rng, fallback_interval)
        if series:
            return series
    return []


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


# --------------------------------------------------------------------- fallback: CoinMarketCap
# Tried ONLY when CoinGecko itself came back with nothing -- see module docstring.
_DEFAULT_COINGECKO_TO_CMC_SYMBOL: dict[str, str] = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
}


def _coingecko_to_cmc_symbols() -> dict[str, str]:
    """CoinGecko's lowercase id -> CoinMarketCap's ticker symbol. Config-overridable
    (`markets.coinmarketcap_symbols`) for the same reason instruments themselves are —
    a custom-configured crypto instrument needs its own mapping to reach this fallback."""
    raw = get_settings().get("markets", "coinmarketcap_symbols", default=None)
    if not raw:
        return _DEFAULT_COINGECKO_TO_CMC_SYMBOL
    return dict(raw)


def _fetch_crypto_coinmarketcap(fetcher: Any, instruments: list[Instrument],
                                api_key: str) -> list[Quote]:
    """Same shape as `_fetch_crypto` (one batched call), a different provider. Yields []
    with no api_key, no instruments, or no known ticker mapping — this is a fallback, and
    "nothing to fall back to" must degrade silently, never look like an error."""
    if not api_key or not instruments:
        return []
    symbol_map = _coingecko_to_cmc_symbols()
    by_cmc_symbol = {symbol_map[i.symbol]: i for i in instruments if i.symbol in symbol_map}
    if not by_cmc_symbol:
        return []
    url = ("https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
          f"?symbol={','.join(by_cmc_symbol)}&convert=USD")
    resp = fetcher.get(url, accept="application/json", headers={"X-CMC_PRO_API_KEY": api_key})
    if resp is None or resp.status_code != 200:
        log.warning("markets: CoinMarketCap fallback unreachable")
        return []
    try:
        data = resp.json()["data"]
    except (ValueError, KeyError, TypeError):
        log.warning("markets: CoinMarketCap fallback returned an unexpected shape")
        return []
    out: list[Quote] = []
    for cmc_symbol, inst in by_cmc_symbol.items():
        row = data.get(cmc_symbol)
        try:
            quote = row["quote"]["USD"]
            price = float(quote["price"])
        except (KeyError, TypeError, ValueError):
            log.warning("markets: no CoinMarketCap data for %s, skipping", inst.name)
            continue
        out.append(Quote(category="crypto", name=inst.name, symbol=inst.symbol,
                         price=price, change_pct=quote.get("percent_change_24h")))
    return out


# --------------------------------------------------------------------- fallback: Alpha Vantage (forex only)
def _fetch_alpha_vantage_forex(fetcher: Any, inst: Instrument, api_key: str) -> Quote | None:
    """Tried ONLY when Yahoo's own forex quote fails. Deliberately NOT extended to
    commodities/stocks — see the module docstring for why neither has an honest 1:1
    mapping. Symbol translation is pattern-driven, never a per-instrument-name lookup:
    Yahoo's forex convention is either a bare 3-letter code meaning "USD to that currency"
    (KES=X) or an explicit 6-letter pair (EURUSD=X, GBPUSD=X)."""
    if not api_key:
        return None
    pair = re.match(r"^([A-Z]{3})([A-Z]{3})=X$", inst.symbol)
    bare = re.match(r"^([A-Z]{3})=X$", inst.symbol)
    if pair:
        from_ccy, to_ccy = pair.group(1), pair.group(2)
    elif bare:
        from_ccy, to_ccy = "USD", bare.group(1)
    else:
        return None
    url = ("https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
          f"&from_currency={from_ccy}&to_currency={to_ccy}&apikey={api_key}")
    resp = fetcher.get(url, accept="application/json")
    if resp is None or resp.status_code != 200:
        log.warning("markets: Alpha Vantage fallback unreachable for %s", inst.name)
        return None
    try:
        rate = float(resp.json()["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
    except (ValueError, KeyError, TypeError):
        log.warning("markets: Alpha Vantage fallback returned an unexpected shape for %s", inst.name)
        return None
    # Alpha Vantage's realtime endpoint carries no % change figure at all — never
    # fabricate one; the quote is honestly price-only when this fallback is what answered.
    return Quote(category="forex", name=inst.name, symbol=inst.symbol, price=rate, change_pct=None)


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
        settings = get_settings()

        crypto_instruments = instruments.get("crypto", [])
        crypto: list[Quote] = []
        try:
            crypto = _fetch_crypto(self.fetcher, crypto_instruments)
        except Exception:  # noqa: BLE001 - one category's failure must not block the rest
            log.warning("markets: crypto fetch failed", exc_info=True)
        if not crypto and crypto_instruments:
            # CoinGecko itself came back empty (down, or rate-limited without a key) —
            # try the keyed fallback before giving up on crypto for this run entirely.
            try:
                crypto = _fetch_crypto_coinmarketcap(
                    self.fetcher, crypto_instruments, settings.coinmarketcap_api_key)
            except Exception:  # noqa: BLE001 - the fallback failing must not block the rest
                log.warning("markets: CoinMarketCap fallback failed", exc_info=True)
        quotes.extend(crypto)

        for category in ("forex", "commodities", "stocks"):
            for inst in instruments.get(category, []):
                q: Quote | None = None
                try:
                    q = _fetch_yahoo_one(self.fetcher, inst, category)
                except Exception:  # noqa: BLE001 - one instrument's failure must not block the rest
                    log.warning("markets: Yahoo fetch failed for %s", inst.name, exc_info=True)
                if q is None and category == "forex":
                    # Yahoo failed on a forex pair specifically — the one category with an
                    # honest 1:1 Alpha Vantage mapping (see module docstring for why
                    # commodities/stocks don't get this fallback at all).
                    try:
                        q = _fetch_alpha_vantage_forex(
                            self.fetcher, inst, settings.alpha_vantage_api_key)
                    except Exception:  # noqa: BLE001 - the fallback failing must not block the rest
                        log.warning("markets: Alpha Vantage fallback failed for %s", inst.name,
                                   exc_info=True)
                if q is not None:
                    quotes.append(q)
        return quotes

    # ------------------------------------------------------------- stage widgets (Phase 38)
    def _ticker_widget(self, quotes: list[Quote]) -> TickerWidget | None:
        """Quotes with no % change at all (rare — only when the source genuinely didn't
        provide one) are left OUT of the ticker rather than shown as a fabricated 0.0%.
        Returns None (never an empty widget) if that leaves nothing to show."""
        now = self._now()
        items = [{"symbol": q.name, "price": q.price, "change_pct": q.change_pct, "as_of": now}
                for q in quotes if q.change_pct is not None]
        return TickerWidget(items=items) if items else None

    def _chart_widget(self, inst: Instrument, category: str, range_key: str) -> ChartWidget | None:
        if inst.source == "coingecko":
            series = _fetch_crypto_chart_series(self.fetcher, inst.symbol, range_key)
            source = "coingecko"
        else:
            series = _fetch_yahoo_chart_series(self.fetcher, inst.symbol, range_key)
            source = "yahoo"
        if not series:
            return None
        return ChartWidget(asset=inst.name, klass=_KLASS_FOR_CATEGORY.get(category, category),
                           range=range_key, series=series, as_of=series[-1]["t"],
                           delayed_label=_DELAYED_LABEL[source], source=source)

    def display(self, asset: str = "", range: str = "1d", **_: Any) -> list[Widget]:
        """Presenter-facing (core/presenter.py): a ticker widget from whatever quotes are
        reachable right now, plus a chart widget for `asset` when it resolves to a
        configured instrument AND a real series is fetchable. No new data source: the
        chart reuses the exact same CoinGecko/Yahoo hosts and robots.txt exception this
        module's docstring already justifies — CoinGecko's own market_chart endpoint
        alongside simple/price, and Yahoo's chart endpoint's series instead of only its
        `.meta`. Never synthesizes a tick: an unresolved asset, or one with no fetchable
        series, means that ONE widget is omitted, never faked — the ticker still renders
        on its own quotes regardless.
        """
        widgets: list[Widget] = []
        quotes = self._fetch_all()
        ticker = self._ticker_widget(quotes) if quotes else None
        if ticker is not None:
            widgets.append(ticker)
        inst, category = _resolve_asset(asset, _instruments())
        if inst is not None and category is not None:
            chart = self._chart_widget(inst, category, range)
            if chart is not None:
                widgets.append(chart)
        return widgets

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
