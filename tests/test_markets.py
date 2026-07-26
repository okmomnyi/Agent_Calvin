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
        self.requested_headers: list[dict[str, str] | None] = []

    def get(self, url: str, accept: str | None = None, headers: dict[str, str] | None = None):
        self.requested.append(url)
        self.requested_headers.append(headers)
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


# ==================================================== stage widgets (Phase 38)
from skills.markets import (_ASSET_ALIASES, _fetch_crypto_chart_series,  # noqa: E402
                            _fetch_yahoo_chart_series, _resolve_asset)


def _closed_market_response() -> _FakeResponse:
    """The exact shape verified live against the real Yahoo endpoint (2026-07-26): when
    the exchange session is closed (after-hours / weekend), the intraday chart has no
    "timestamp" key at all and an empty indicators.quote -- not malformed, just nothing
    for that window. This is the shape that silently dropped every commodity/stock chart
    in production (CL=F/oil) until the range-widening fallback below was added."""
    return _FakeResponse(200, {"chart": {"result": [{
        "meta": {"regularMarketPrice": 89.31, "previousClose": 92.19},
        "indicators": {"quote": [{}]},
    }], "error": None}})


def _crypto_chart_url(coingecko_id: str, days: int) -> str:
    return (f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart"
            f"?vs_currency=usd&days={days}")


def _yahoo_chart_url(symbol: str, rng: str, interval: str) -> str:
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval={interval}"


def test_fetch_crypto_chart_series_parses_prices():
    url = _crypto_chart_url("bitcoin", 1)
    fetcher = _FakeFetcher({url: _FakeResponse(200, {"prices": [[1000, 60000.0], [2000, 60100.0]]})})
    series = _fetch_crypto_chart_series(fetcher, "bitcoin", "1d")
    assert series == [{"t": 1.0, "v": 60000.0}, {"t": 2.0, "v": 60100.0}]


def test_fetch_crypto_chart_series_uses_the_range_specific_days_param():
    url = _crypto_chart_url("bitcoin", 30)
    fetcher = _FakeFetcher({url: _FakeResponse(200, {"prices": [[1000, 1.0]]})})
    assert _fetch_crypto_chart_series(fetcher, "bitcoin", "1m") == [{"t": 1.0, "v": 1.0}]


def test_fetch_crypto_chart_series_degrades_to_empty_on_unreachable_endpoint():
    assert _fetch_crypto_chart_series(_FakeFetcher({}), "bitcoin", "1d") == []


def test_fetch_crypto_chart_series_degrades_to_empty_on_malformed_payload():
    url = _crypto_chart_url("bitcoin", 1)
    fetcher = _FakeFetcher({url: _FakeResponse(200, {"not_prices": []})})
    assert _fetch_crypto_chart_series(fetcher, "bitcoin", "1d") == []


def test_fetch_crypto_chart_series_drops_null_points_never_interpolates():
    url = _crypto_chart_url("bitcoin", 1)
    fetcher = _FakeFetcher({url: _FakeResponse(200, {"prices": [[1000, 60000.0], [2000, None]]})})
    assert _fetch_crypto_chart_series(fetcher, "bitcoin", "1d") == [{"t": 1.0, "v": 60000.0}]


def test_fetch_yahoo_chart_series_parses_timestamps_and_closes():
    url = _yahoo_chart_url("GC=F", "1d", "5m")
    payload = {"chart": {"result": [{
        "timestamp": [100, 200, 300],
        "indicators": {"quote": [{"close": [2000.0, 2001.0, 2002.0]}]},
    }]}}
    fetcher = _FakeFetcher({url: _FakeResponse(200, payload)})
    series = _fetch_yahoo_chart_series(fetcher, "GC=F", "1d")
    assert series == [{"t": 100.0, "v": 2000.0}, {"t": 200.0, "v": 2001.0}, {"t": 300.0, "v": 2002.0}]


def test_fetch_yahoo_chart_series_drops_null_closes_never_interpolates():
    url = _yahoo_chart_url("GC=F", "1d", "5m")
    payload = {"chart": {"result": [{
        "timestamp": [100, 200],
        "indicators": {"quote": [{"close": [2000.0, None]}]},
    }]}}
    fetcher = _FakeFetcher({url: _FakeResponse(200, payload)})
    assert _fetch_yahoo_chart_series(fetcher, "GC=F", "1d") == [{"t": 100.0, "v": 2000.0}]


def test_fetch_yahoo_chart_series_uses_range_specific_params_for_1w():
    url = _yahoo_chart_url("GC=F", "5d", "30m")
    fetcher = _FakeFetcher({url: _FakeResponse(200, {
        "chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"close": [1.0]}]}}]}})})
    assert _fetch_yahoo_chart_series(fetcher, "GC=F", "1w") == [{"t": 1.0, "v": 1.0}]


def test_fetch_yahoo_chart_series_degrades_to_empty_on_unreachable_endpoint():
    assert _fetch_yahoo_chart_series(_FakeFetcher({}), "GC=F", "1d") == []


def test_fetch_yahoo_chart_series_degrades_to_empty_on_malformed_payload():
    url = _yahoo_chart_url("GC=F", "1d", "5m")
    fetcher = _FakeFetcher({url: _FakeResponse(200, {"chart": {"result": []}})})
    assert _fetch_yahoo_chart_series(fetcher, "GC=F", "1d") == []


# ------------------------------------------------------------------ closed-market fallback
# Regression for a REAL production bug found live (2026-07-26): "show oil" produced a
# ticker but no chart, because CL=F's 1d/5m intraday window was empty (the futures session
# was closed) and the old code had no fallback at all -- _chart_widget silently returned
# None. Verified against the actual Yahoo endpoint before fixing.
def test_fetch_yahoo_chart_series_falls_back_to_5d_daily_when_the_intraday_window_is_empty():
    primary_url = _yahoo_chart_url("CL=F", "1d", "5m")
    fallback_url = _yahoo_chart_url("CL=F", "5d", "1d")
    fetcher = _FakeFetcher({
        primary_url: _closed_market_response(),
        fallback_url: _FakeResponse(200, {"chart": {"result": [{
            "timestamp": [100, 200, 300],
            "indicators": {"quote": [{"close": [88.0, 89.0, 89.31]}]},
        }]}}),
    })

    series = _fetch_yahoo_chart_series(fetcher, "CL=F", "1d")

    assert series == [{"t": 100.0, "v": 88.0}, {"t": 200.0, "v": 89.0}, {"t": 300.0, "v": 89.31}]


def test_fetch_yahoo_chart_series_falls_back_further_to_1mo_daily_when_5d_is_also_empty():
    fetcher = _FakeFetcher({
        _yahoo_chart_url("CL=F", "1d", "5m"): _closed_market_response(),
        _yahoo_chart_url("CL=F", "5d", "1d"): _closed_market_response(),
        _yahoo_chart_url("CL=F", "1mo", "1d"): _FakeResponse(200, {"chart": {"result": [{
            "timestamp": [500], "indicators": {"quote": [{"close": [90.5]}]},
        }]}}),
    })

    series = _fetch_yahoo_chart_series(fetcher, "CL=F", "1d")

    assert series == [{"t": 500.0, "v": 90.5}]


def test_fetch_yahoo_chart_series_never_synthesizes_when_every_window_is_empty():
    """No real tick anywhere -- no chart, never a fabricated one, even after exhausting
    every fallback window."""
    fetcher = _FakeFetcher({
        _yahoo_chart_url("CL=F", "1d", "5m"): _closed_market_response(),
        _yahoo_chart_url("CL=F", "5d", "1d"): _closed_market_response(),
        _yahoo_chart_url("CL=F", "1mo", "1d"): _closed_market_response(),
    })
    assert _fetch_yahoo_chart_series(fetcher, "CL=F", "1d") == []


def test_fetch_yahoo_chart_series_does_not_re_request_a_fallback_identical_to_the_primary():
    """1w's primary window IS 5d/30m -- the 5d/1d fallback is still a distinct request
    (different interval), so this just confirms no duplicate/wasted request against the
    exact same (range, interval) pair already tried."""
    calls = []
    real_fetcher = _FakeFetcher({
        _yahoo_chart_url("CL=F", "5d", "30m"): _closed_market_response(),
        _yahoo_chart_url("CL=F", "5d", "1d"): _FakeResponse(200, {"chart": {"result": [{
            "timestamp": [1], "indicators": {"quote": [{"close": [1.0]}]},
        }]}}),
    })

    class _Tracking:
        def get(self, url, accept=None):
            calls.append(url)
            return real_fetcher.get(url, accept)

    series = _fetch_yahoo_chart_series(_Tracking(), "CL=F", "1w")

    assert series == [{"t": 1.0, "v": 1.0}]
    assert len(calls) == len(set(calls))  # no URL requested twice


# ==================================================== fallback: CoinMarketCap (crypto)
from skills.markets import (_fetch_alpha_vantage_forex,  # noqa: E402
                            _fetch_crypto_coinmarketcap)

_CMC_URL = ("https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
           "?symbol=BTC&convert=USD")


def _cmc_payload(price: float, change_24h: float | None) -> dict:
    quote = {"price": price}
    if change_24h is not None:
        quote["percent_change_24h"] = change_24h
    return {"data": {"BTC": {"quote": {"USD": quote}}}}


def test_coinmarketcap_fallback_parses_price_and_change():
    fetcher = _FakeFetcher({_CMC_URL: _FakeResponse(200, _cmc_payload(65000.0, 2.5))})
    quotes = _fetch_crypto_coinmarketcap(
        fetcher, [Instrument("Bitcoin", "bitcoin", "coingecko")], api_key="cmc-test-key")

    assert quotes == [Quote(category="crypto", name="Bitcoin", symbol="bitcoin",
                            price=65000.0, change_pct=2.5)]


def test_coinmarketcap_fallback_sends_the_key_as_a_header_not_a_query_param():
    fetcher = _FakeFetcher({_CMC_URL: _FakeResponse(200, _cmc_payload(65000.0, 2.5))})
    _fetch_crypto_coinmarketcap(fetcher, [Instrument("Bitcoin", "bitcoin", "coingecko")],
                               api_key="cmc-test-key")

    assert "cmc-test-key" not in fetcher.requested[0]  # never leaked into the URL/logs
    assert fetcher.requested_headers[0] == {"X-CMC_PRO_API_KEY": "cmc-test-key"}


def test_coinmarketcap_fallback_with_no_api_key_is_a_silent_noop():
    fetcher = _FakeFetcher({_CMC_URL: _FakeResponse(200, _cmc_payload(65000.0, 2.5))})
    quotes = _fetch_crypto_coinmarketcap(
        fetcher, [Instrument("Bitcoin", "bitcoin", "coingecko")], api_key="")
    assert quotes == []
    assert fetcher.requested == []  # never even calls out with no key


def test_coinmarketcap_fallback_with_no_instruments_is_a_noop():
    assert _fetch_crypto_coinmarketcap(_FakeFetcher({}), [], api_key="k") == []


def test_coinmarketcap_fallback_skips_an_instrument_with_no_known_ticker_mapping():
    fetcher = _FakeFetcher({})
    quotes = _fetch_crypto_coinmarketcap(
        fetcher, [Instrument("Some New Coin", "some-new-coin", "coingecko")], api_key="k")
    assert quotes == []
    assert fetcher.requested == []  # no mapping -> never even calls out


def test_coinmarketcap_fallback_degrades_to_empty_on_unreachable_endpoint():
    assert _fetch_crypto_coinmarketcap(
        _FakeFetcher({}), [Instrument("Bitcoin", "bitcoin", "coingecko")], api_key="k") == []


def test_coinmarketcap_fallback_degrades_to_empty_on_malformed_payload():
    fetcher = _FakeFetcher({_CMC_URL: _FakeResponse(200, {"data": {}})})
    quotes = _fetch_crypto_coinmarketcap(
        fetcher, [Instrument("Bitcoin", "bitcoin", "coingecko")], api_key="k")
    assert quotes == []


def test_snapshot_falls_back_to_coinmarketcap_when_coingecko_is_down(monkeypatch):
    _force_default_instruments(monkeypatch)
    settings = type("S", (), {
        "get": staticmethod(lambda *a, default=None, **k: default),
        "coinmarketcap_api_key": "cmc-test-key", "alpha_vantage_api_key": "",
    })()
    monkeypatch.setattr("skills.markets.get_settings", lambda: settings)

    fetcher = _fetcher_for({
        "KES=X": (129.4, 129.0), "EURUSD=X": (1.10, 1.10), "GBPUSD=X": (1.30, 1.30),
        "GC=F": (2050.0, 2000.0), "CL=F": (75.0, 75.0),
        "^GSPC": (5000.0, 5000.0), "^IXIC": (16000.0, 16000.0),
    }, [])  # no crypto entries registered -> CoinGecko returns []
    cmc_all_url = ("https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
                  "?symbol=BTC,ETH,SOL&convert=USD")
    fetcher._by_url[cmc_all_url] = _FakeResponse(
        200, {"data": {
            "BTC": {"quote": {"USD": {"price": 64000.0, "percent_change_24h": 1.0}}},
            "ETH": {"quote": {"USD": {"price": 1800.0, "percent_change_24h": 0.5}}},
            "SOL": {"quote": {"USD": {"price": 70.0, "percent_change_24h": 0.2}}},
        }})
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    result = skill.snapshot(notify=False)

    assert result.ok
    assert "crypto" in result.data["categories"]


# ==================================================== fallback: Alpha Vantage (forex only)
_AV_URL_KES = ("https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
              "&from_currency=USD&to_currency=KES&apikey=av-test-key")


def test_alpha_vantage_fallback_parses_a_bare_usd_pair():
    inst = Instrument("USD/KES", "KES=X", "yahoo")
    fetcher = _FakeFetcher({_AV_URL_KES: _FakeResponse(
        200, {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "129.55"}})})

    q = _fetch_alpha_vantage_forex(fetcher, inst, api_key="av-test-key")

    assert q == Quote(category="forex", name="USD/KES", symbol="KES=X",
                      price=129.55, change_pct=None)


def test_alpha_vantage_fallback_parses_an_explicit_six_letter_pair():
    inst = Instrument("USD/EUR", "EURUSD=X", "yahoo")
    url = ("https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
          "&from_currency=EUR&to_currency=USD&apikey=av-test-key")
    fetcher = _FakeFetcher({url: _FakeResponse(
        200, {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "1.10"}})})

    q = _fetch_alpha_vantage_forex(fetcher, inst, api_key="av-test-key")

    assert q is not None
    assert q.price == 1.10


def test_alpha_vantage_fallback_never_fabricates_a_change_pct():
    """The realtime endpoint carries no % change figure at all -- must stay None, never
    a guessed/interpolated value."""
    inst = Instrument("USD/KES", "KES=X", "yahoo")
    fetcher = _FakeFetcher({_AV_URL_KES: _FakeResponse(
        200, {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "129.55"}})})
    q = _fetch_alpha_vantage_forex(fetcher, inst, api_key="av-test-key")
    assert q.change_pct is None


def test_alpha_vantage_fallback_with_no_api_key_is_a_silent_noop():
    inst = Instrument("USD/KES", "KES=X", "yahoo")
    fetcher = _FakeFetcher({_AV_URL_KES: _FakeResponse(
        200, {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "129.55"}})})
    assert _fetch_alpha_vantage_forex(fetcher, inst, api_key="") is None
    assert fetcher.requested == []


def test_alpha_vantage_fallback_is_never_attempted_for_commodities_or_stocks():
    """No honest mapping exists for gold/oil (no spot function) or an index (an ETF
    proxy would be a DIFFERENT number under the same label) -- must refuse, not guess."""
    gold = Instrument("Gold", "GC=F", "yahoo")
    sp500 = Instrument("S&P 500", "^GSPC", "yahoo")
    fetcher = _FakeFetcher({})

    assert _fetch_alpha_vantage_forex(fetcher, gold, api_key="k") is None
    assert _fetch_alpha_vantage_forex(fetcher, sp500, api_key="k") is None
    assert fetcher.requested == []


def test_alpha_vantage_fallback_degrades_to_none_on_unreachable_endpoint():
    inst = Instrument("USD/KES", "KES=X", "yahoo")
    assert _fetch_alpha_vantage_forex(_FakeFetcher({}), inst, api_key="k") is None


def test_alpha_vantage_fallback_degrades_to_none_on_malformed_payload():
    inst = Instrument("USD/KES", "KES=X", "yahoo")
    fetcher = _FakeFetcher({_AV_URL_KES: _FakeResponse(200, {"unexpected": "shape"})})
    assert _fetch_alpha_vantage_forex(fetcher, inst, api_key="av-test-key") is None


def test_snapshot_falls_back_to_alpha_vantage_only_for_the_failing_forex_instrument(monkeypatch):
    """Yahoo fails for ONE forex pair; Alpha Vantage covers just that one, everything else
    (including the other forex pairs that Yahoo answered fine) is untouched."""
    _force_default_instruments(monkeypatch)
    settings = type("S", (), {
        "get": staticmethod(lambda *a, default=None, **k: default),
        "coinmarketcap_api_key": "", "alpha_vantage_api_key": "av-test-key",
    })()
    monkeypatch.setattr("skills.markets.get_settings", lambda: settings)

    fetcher = _fetcher_for({
        "bitcoin": (64000.0, 1.0), "ethereum": (1800.0, 0.5), "solana": (70.0, 0.2),
        "EURUSD=X": (1.10, 1.10), "GBPUSD=X": (1.30, 1.30),
        "GC=F": (2050.0, 2000.0), "CL=F": (75.0, 75.0),
        "^GSPC": (5000.0, 5000.0), "^IXIC": (16000.0, 16000.0),
        # KES=X deliberately NOT registered -> Yahoo fetch returns None for it
    }, DEFAULT_INSTRUMENTS["crypto"])
    fetcher._by_url[_AV_URL_KES] = _FakeResponse(
        200, {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "129.55"}})
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    result = skill.snapshot(notify=False)

    assert result.ok
    assert result.data["quote_count"] == 10  # all instruments present, KES=X via the fallback
    assert "129.55" in result.text or "129.6" in result.text  # rendered with the AV rate


# ------------------------------------------------------------------ asset resolution
def test_resolve_asset_matches_via_alias():
    inst, category = _resolve_asset("show oil", DEFAULT_INSTRUMENTS)
    assert inst is not None and inst.name == "Crude Oil (WTI)"
    assert category == "commodities"


def test_resolve_asset_matches_via_plain_substring():
    inst, category = _resolve_asset("what about gold today", DEFAULT_INSTRUMENTS)
    assert inst is not None and inst.name == "Gold"
    assert category == "commodities"


def test_resolve_asset_returns_none_for_an_unresolvable_asset():
    assert _resolve_asset("underwater basket weaving", DEFAULT_INSTRUMENTS) == (None, None)


def test_resolve_asset_returns_none_for_a_blank_hint():
    assert _resolve_asset("", DEFAULT_INSTRUMENTS) == (None, None)


def test_every_asset_alias_target_actually_exists_in_default_instruments():
    """A stale alias (renamed/removed instrument) would silently never resolve again."""
    for cat_key, name_key in _ASSET_ALIASES.values():
        names = [i.name for i in DEFAULT_INSTRUMENTS.get(cat_key, [])]
        assert name_key in names, f"{cat_key}/{name_key} not in DEFAULT_INSTRUMENTS"


# ------------------------------------------------------------------ display() end to end
def test_display_ticker_omits_quotes_with_no_change_pct():
    skill = MarketsSkill(fetcher=_FakeFetcher({}), news_skill=_FakeNews())
    quotes = [Quote(category="crypto", name="Bitcoin", symbol="bitcoin", price=1.0, change_pct=None),
             Quote(category="crypto", name="Ethereum", symbol="ethereum", price=2.0, change_pct=1.0)]
    widget = skill._ticker_widget(quotes)
    assert widget is not None
    assert [i["symbol"] for i in widget.items] == ["Ethereum"]


def test_display_ticker_widget_is_none_when_every_quote_lacks_change_pct():
    skill = MarketsSkill(fetcher=_FakeFetcher({}), news_skill=_FakeNews())
    quotes = [Quote(category="crypto", name="Bitcoin", symbol="bitcoin", price=1.0, change_pct=None)]
    assert skill._ticker_widget(quotes) is None


def test_display_with_no_asset_returns_ticker_only(monkeypatch):
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({"bitcoin": (64000.0, 1.0)}, DEFAULT_INSTRUMENTS["crypto"])
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    widgets = skill.display()

    assert len(widgets) == 1
    assert widgets[0].type == "ticker"


def test_display_omits_chart_for_an_unresolvable_asset(monkeypatch):
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({"bitcoin": (64000.0, 1.0)}, DEFAULT_INSTRUMENTS["crypto"])
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    widgets = skill.display(asset="underwater basket weaving")

    assert [w.type for w in widgets] == ["ticker"]


def test_display_includes_chart_with_as_of_and_delayed_label_for_a_resolved_asset(monkeypatch):
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({"bitcoin": (64000.0, 1.0)}, DEFAULT_INSTRUMENTS["crypto"])
    fetcher._by_url[_crypto_chart_url("bitcoin", 1)] = _FakeResponse(
        200, {"prices": [[1000, 64000.0], [2000, 64100.0]]})
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    widgets = skill.display(asset="bitcoin")

    chart = next(w for w in widgets if w.type == "chart")
    assert chart.asset == "Bitcoin"
    assert chart.klass == "crypto"
    assert chart.source == "coingecko"
    assert chart.delayed_label == "real-time"
    assert chart.as_of == 2.0
    assert chart.series == [{"t": 1.0, "v": 64000.0}, {"t": 2.0, "v": 64100.0}]


def test_display_omits_chart_for_a_resolved_asset_with_no_fetchable_series(monkeypatch):
    """The hostile case named in the spec: no data means no chart, never a fabricated
    series, even though the asset itself resolved fine."""
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({"bitcoin": (64000.0, 1.0)}, DEFAULT_INSTRUMENTS["crypto"])
    # No market_chart entry registered for "bitcoin" -> fetcher.get() returns None.
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    widgets = skill.display(asset="bitcoin")

    assert all(w.type != "chart" for w in widgets)


def test_display_yahoo_chart_carries_the_delayed_label(monkeypatch):
    _force_default_instruments(monkeypatch)
    fetcher = _fetcher_for({"GC=F": (2050.0, 2000.0)}, [])
    fetcher._by_url[_yahoo_chart_url("GC=F", "1d", "5m")] = _FakeResponse(200, {
        "chart": {"result": [{"timestamp": [1.0], "indicators": {"quote": [{"close": [2050.0]}]}}]}})
    skill = MarketsSkill(fetcher=fetcher, news_skill=_FakeNews())

    widgets = skill.display(asset="gold")

    chart = next(w for w in widgets if w.type == "chart")
    assert chart.source == "yahoo"
    assert chart.delayed_label == "~15m delayed (free feed)"
    assert chart.klass == "commodity"
