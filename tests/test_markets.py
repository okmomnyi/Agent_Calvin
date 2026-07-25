"""Markets snapshot skill: quote fetching, per-source/per-instrument degrade, % change
math, notable-move gating, news-correlation synthesis fallback, and injectable notify
(nothing in this suite may reach a real Telegram/CoinGecko/Yahoo endpoint)."""

from __future__ import annotations

import json

from skills.markets import (DEFAULT_INSTRUMENTS, Instrument, MarketsSkill, Quote,
                            _fetch_crypto, _fetch_yahoo_one, _instruments)
from skills.world_news import Headline


class _FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class _FakeFetcher:
    def __init__(self, by_url: dict[str, _FakeResponse]) -> None:
        self._by_url = by_url
        self.requested: list[str] = []

    def get(self, url: str, accept: str | None = None):
        self.requested.append(url)
        return self._by_url.get(url)


class _FakeNews:
    """Stands in for WorldNewsSkill — only recent_headlines() is exercised here."""

    def __init__(self, by_category: dict[str, list[Headline]] | None = None,
                 raise_for: set[str] | None = None) -> None:
        self._by_category = by_category or {}
        self._raise_for = raise_for or set()

    def recent_headlines(self, category: str) -> list[Headline]:
        if category in self._raise_for:
            raise RuntimeError("news fetch broke")
        return self._by_category.get(category, [])


def _coingecko_url(instruments: list[Instrument]) -> str:
    ids = ",".join(i.symbol for i in instruments)
    return f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"


def _yahoo_url(symbol: str) -> str:
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _yahoo_payload(price: float, prev_close: float | None) -> dict:
    meta = {"regularMarketPrice": price}
    if prev_close is not None:
        meta["previousClose"] = prev_close
    return {"chart": {"result": [{"meta": meta}]}}


def _headline(source: str, title: str) -> Headline:
    return Headline(category="business", source=source, title=title,
                    url=f"https://x.test/{title}", published_at=None, group=source)


# ------------------------------------------------------------------ instrument config
def test_instruments_falls_back_to_defaults_when_config_missing(monkeypatch):
    monkeypatch.setattr("skills.markets.get_settings",
                        lambda: type("S", (), {"get": staticmethod(lambda *a, **k: None)})())
    assert _instruments() == DEFAULT_INSTRUMENTS


# ------------------------------------------------------------------ CoinGecko fetch
def test_fetch_crypto_parses_price_and_24h_change():
    instruments = [Instrument("Bitcoin", "bitcoin", "coingecko")]
    fetcher = _FakeFetcher({
        _coingecko_url(instruments): _FakeResponse(
            200, {"bitcoin": {"usd": 64000.0, "usd_24h_change": 1.23}}),
    })
    quotes = _fetch_crypto(fetcher, instruments)
    assert len(quotes) == 1
    assert quotes[0] == Quote(category="crypto", name="Bitcoin", symbol="bitcoin",
                              price=64000.0, change_pct=1.23)


def test_fetch_crypto_degrades_on_unreachable_endpoint():
    instruments = [Instrument("Bitcoin", "bitcoin", "coingecko")]
    fetcher = _FakeFetcher({})  # no entry -> .get() returns None
    assert _fetch_crypto(fetcher, instruments) == []


def test_fetch_crypto_degrades_on_non_200():
    instruments = [Instrument("Bitcoin", "bitcoin", "coingecko")]
    fetcher = _FakeFetcher({_coingecko_url(instruments): _FakeResponse(503)})
    assert _fetch_crypto(fetcher, instruments) == []


def test_fetch_crypto_skips_one_missing_id_but_keeps_the_rest():
    instruments = [Instrument("Bitcoin", "bitcoin", "coingecko"),
                   Instrument("Ethereum", "ethereum", "coingecko")]
    fetcher = _FakeFetcher({
        _coingecko_url(instruments): _FakeResponse(
            200, {"bitcoin": {"usd": 64000.0, "usd_24h_change": 1.0}}),
            # "ethereum" missing from the payload entirely
    })
    quotes = _fetch_crypto(fetcher, instruments)
    assert [q.name for q in quotes] == ["Bitcoin"]


def test_fetch_crypto_returns_empty_on_no_instruments():
    assert _fetch_crypto(_FakeFetcher({}), []) == []


# ------------------------------------------------------------------ Yahoo fetch
def test_fetch_yahoo_one_computes_change_pct_from_previous_close():
    inst = Instrument("Gold", "GC=F", "yahoo")
    fetcher = _FakeFetcher({_yahoo_url("GC=F"): _FakeResponse(200, _yahoo_payload(2050.0, 2000.0))})
    q = _fetch_yahoo_one(fetcher, inst, "commodities")
    assert q.name == "Gold"
    assert q.price == 2050.0
    assert abs(q.change_pct - 2.5) < 1e-9


def test_fetch_yahoo_one_handles_missing_previous_close():
    inst = Instrument("Gold", "GC=F", "yahoo")
    fetcher = _FakeFetcher({_yahoo_url("GC=F"): _FakeResponse(200, _yahoo_payload(2050.0, None))})
    q = _fetch_yahoo_one(fetcher, inst, "commodities")
    assert q.price == 2050.0
    assert q.change_pct is None


def test_fetch_yahoo_one_degrades_on_unreachable_endpoint():
    inst = Instrument("Gold", "GC=F", "yahoo")
    assert _fetch_yahoo_one(_FakeFetcher({}), inst, "commodities") is None


def test_fetch_yahoo_one_degrades_on_malformed_payload():
    inst = Instrument("Gold", "GC=F", "yahoo")
    fetcher = _FakeFetcher({_yahoo_url("GC=F"): _FakeResponse(200, {"chart": {"result": []}})})
    assert _fetch_yahoo_one(fetcher, inst, "commodities") is None


# ------------------------------------------------------------------ snapshot() end-to-end
def _fetcher_for(quotes_by_symbol: dict[str, tuple[float, float | None]],
                 crypto_instruments: list[Instrument]) -> _FakeFetcher:
    by_url = {}
    crypto_payload = {}
    for inst in crypto_instruments:
        if inst.symbol in quotes_by_symbol:
            price, prev = quotes_by_symbol[inst.symbol]
            crypto_payload[inst.symbol] = {"usd": price, "usd_24h_change": prev}
    if crypto_instruments:
        by_url[_coingecko_url(crypto_instruments)] = _FakeResponse(200, crypto_payload)
    for symbol, (price, prev) in quotes_by_symbol.items():
        if symbol not in crypto_payload:
            by_url[_yahoo_url(symbol)] = _FakeResponse(200, _yahoo_payload(price, prev))
    return _FakeFetcher(by_url)


def _force_default_instruments(monkeypatch) -> None:
    """Config.yaml currently mirrors DEFAULT_INSTRUMENTS, but a test asserting exact fetch
    URLs must not depend on that staying true — force _instruments() to its documented
    fallback so these tests can't be broken by an unrelated config edit."""
    monkeypatch.setattr("skills.markets.get_settings",
                        lambda: type("S", (), {"get": staticmethod(
                            lambda *a, default=None, **k: default)})())


def test_snapshot_renders_all_categories_and_disclaimer(monkeypatch):
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({
        "bitcoin": (64000.0, 1.0), "ethereum": (1800.0, 0.5), "solana": (70.0, 0.2),
        "KES=X": (129.4, 129.0), "EURUSD=X": (1.10, 1.10), "GBPUSD=X": (1.30, 1.30),
        "GC=F": (2050.0, 2000.0), "CL=F": (75.0, 75.0),
        "^GSPC": (5000.0, 5000.0), "^IXIC": (16000.0, 16000.0),
    }, DEFAULT_INSTRUMENTS["crypto"])
    notified = []
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews(),
                         notify=lambda t: notified.append(t) or True)

    result = skill.snapshot(notify=True)

    assert result.ok
    assert result.data["quote_count"] == 10
    assert set(result.data["categories"]) == {"crypto", "forex", "commodities", "stocks"}
    assert "not financial advice" in result.text
    assert "predictions" in result.text
    assert notified == [result.text]


def test_snapshot_degrades_gracefully_when_every_source_is_down():
    skill = MarketsSkill(fetcher=_FakeFetcher({}), news_skill=_FakeNews(), notify=lambda t: True)
    result = skill.snapshot(notify=False)
    assert result.ok is False
    assert result.data["quote_count"] == 0
    assert "Couldn't reach" in result.text


def test_snapshot_never_calls_notify_transport_when_notify_false(monkeypatch):
    _force_default_instruments(monkeypatch)

    def _boom(text):
        raise AssertionError("must not notify when notify=False")

    fetcher = _fetcher_for({
        "bitcoin": (64000.0, 1.0), "ethereum": (1800.0, 0.5), "solana": (70.0, 0.2),
    }, DEFAULT_INSTRUMENTS["crypto"])
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews(), notify=_boom)
    result = skill.snapshot(notify=False)
    assert result.ok


# ------------------------------------------------------------------ news correlation
def test_correlate_returns_empty_when_no_notable_movers(fake_llm):
    skill = MarketsSkill(llm=fake_llm)
    quotes = [Quote(category="crypto", name="Bitcoin", symbol="bitcoin", price=64000.0, change_pct=0.1)]
    headlines = [_headline("Reuters", "Some story")]
    assert skill._correlate(quotes, headlines) == ""
    assert fake_llm.calls == []  # never even calls the model for a flat market


def test_correlate_returns_empty_when_no_headlines_available(fake_llm):
    skill = MarketsSkill(llm=fake_llm)
    quotes = [Quote(category="crypto", name="Bitcoin", symbol="bitcoin", price=64000.0, change_pct=9.0)]
    assert skill._correlate(quotes, []) == ""
    assert fake_llm.calls == []


def test_correlate_calls_llm_with_movers_and_headlines(fake_llm):
    fake_llm.post_result = "Gold rose amid reports of Middle East tensions (Reuters)."
    skill = MarketsSkill(llm=fake_llm)
    quotes = [Quote(category="commodities", name="Gold", symbol="GC=F", price=2050.0, change_pct=3.5),
             Quote(category="crypto", name="Bitcoin", symbol="bitcoin", price=64000.0, change_pct=0.1)]
    headlines = [_headline("Reuters", "Middle East tensions rise")]

    commentary = skill._correlate(quotes, headlines)

    assert commentary == "Gold rose amid reports of Middle East tensions (Reuters)."
    assert len(fake_llm.calls) == 1
    # index 0 is the grounding system message LLMClient.chat() prepends (current-time/
    # evidence rules), index 1 is markets.py's own system prompt, index 2 the user context.
    user_content = fake_llm.calls[0]["messages"][2]["content"]
    assert "Gold" in user_content
    assert "Bitcoin" not in user_content  # not a notable mover, excluded from the prompt
    assert "Middle East tensions rise" in user_content


def test_correlate_falls_back_to_empty_on_llm_error(monkeypatch, fake_llm):
    from core.llm import LLMError

    def _raise(*a, **k):
        raise LLMError("nim down")

    monkeypatch.setattr(fake_llm, "chat", _raise, raising=False)
    skill = MarketsSkill(llm=fake_llm)
    quotes = [Quote(category="commodities", name="Gold", symbol="GC=F", price=2050.0, change_pct=3.5)]
    headlines = [_headline("Reuters", "Middle East tensions rise")]
    assert skill._correlate(quotes, headlines) == ""


def test_headlines_for_context_degrades_per_category():
    news = _FakeNews(by_category={"world": [_headline("BBC", "A world story")]},
                     raise_for={"business"})
    skill = MarketsSkill(news_skill=news)
    headlines = skill._headlines_for_context()
    assert [h.title for h in headlines] == ["A world story"]


def test_headlines_for_context_caps_total_count():
    many = [_headline("BBC", f"story {i}") for i in range(30)]
    news = _FakeNews(by_category={"business": many})
    skill = MarketsSkill(news_skill=news)
    headlines = skill._headlines_for_context()
    assert len(headlines) == 20
