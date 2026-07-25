"""World-news briefing skill: RSS parsing, per-source degrade, freshness window, synthesis
fallback, and injectable notify (nothing in this suite may reach a real Telegram/RSS feed)."""

from __future__ import annotations

import time

from skills.world_news import FEEDS, Headline, WorldNewsSkill, _parse_rss

_RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Fake Feed</title>
{items}
</channel></rss>"""

_ITEM_TEMPLATE = """<item>
<title>{title}</title>
<link>{link}</link>
<pubDate>{pub_date}</pubDate>
</item>"""


def _rss(items: list[tuple[str, str, str]]) -> str:
    body = "\n".join(_ITEM_TEMPLATE.format(title=t, link=l, pub_date=p) for t, l, p in items)
    return _RSS_TEMPLATE.format(items=body)


def _rfc822(ts: float) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeFetcher:
    def __init__(self, by_url: dict[str, _FakeResponse]) -> None:
        self._by_url = by_url
        self.requested: list[str] = []

    def get(self, url: str, accept: str | None = None):
        self.requested.append(url)
        return self._by_url.get(url)


# ------------------------------------------------------------------ RSS parsing
def test_parse_rss_extracts_title_link_and_pubdate():
    now = time.time()
    xml = _rss([("Big story", "https://x.test/1", _rfc822(now))])
    headlines = _parse_rss(xml, source="Fake", category="world")
    assert len(headlines) == 1
    h = headlines[0]
    assert h.title == "Big story"
    assert h.url == "https://x.test/1"
    assert h.source == "Fake"
    assert h.category == "world"
    assert abs(h.published_at - now) < 2


def test_parse_rss_skips_items_missing_title_or_link():
    xml = _rss([("", "https://x.test/1", _rfc822(time.time())),
                ("Has title, no link", "", _rfc822(time.time()))])
    assert _parse_rss(xml, "Fake", "world") == []


def test_parse_rss_tolerates_malformed_xml():
    assert _parse_rss("<not valid xml", "Fake", "world") == []


def test_parse_rss_keeps_items_with_an_unparseable_pubdate():
    xml = _rss([("Weird date", "https://x.test/1", "not-a-real-date")])
    headlines = _parse_rss(xml, "Fake", "world")
    assert len(headlines) == 1
    assert headlines[0].published_at is None


# ------------------------------------------------------------------ fetch + freshness window
def test_fetch_category_drops_items_outside_the_24h_window():
    now = 2_000_000.0
    fresh_xml = _rss([("Fresh", "https://x.test/fresh", _rfc822(now - 3600))])
    stale_xml = _rss([("Stale", "https://x.test/stale", _rfc822(now - 3 * 24 * 3600))])
    urls = FEEDS["world"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, fresh_xml),
        urls[1][1]: _FakeResponse(200, stale_xml),
    })
    skill = WorldNewsSkill(fetcher=fetcher, clock=lambda: now)

    headlines = skill._fetch_category("world")

    titles = [h.title for h in headlines]
    assert "Fresh" in titles
    assert "Stale" not in titles


def test_fetch_category_degrades_when_one_source_is_unreachable():
    now = 2_000_000.0
    urls = FEEDS["world"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Alive", "https://x.test/a", _rfc822(now))])),
        # urls[1] intentionally missing -> fetcher.get() returns None
    })
    skill = WorldNewsSkill(fetcher=fetcher, clock=lambda: now)

    headlines = skill._fetch_category("world")

    assert [h.title for h in headlines] == ["Alive"]


def test_fetch_category_degrades_on_a_non_200(monkeypatch):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({urls[0][1]: _FakeResponse(503, "")})
    skill = WorldNewsSkill(fetcher=fetcher, clock=lambda: now)

    assert skill._fetch_category("kenya") == []


# ------------------------------------------------------------------ synthesis
def test_synthesize_with_no_headlines_says_so_without_calling_the_llm(fake_llm):
    skill = WorldNewsSkill(llm=fake_llm)
    text = skill._synthesize([])
    assert "nothing fresh" in text.lower()
    assert fake_llm.calls == []


def test_synthesize_calls_the_llm_with_the_headlines_as_context(fake_llm):
    fake_llm.post_result = "AgentOS 5 launched today. (TechCrunch)"
    skill = WorldNewsSkill(llm=fake_llm)
    headlines = [Headline(category="tech_ai", source="TechCrunch", title="AgentOS 5 launched",
                          url="https://x.test/1", published_at=time.time())]

    text = skill._synthesize(headlines)

    assert text == "AgentOS 5 launched today. (TechCrunch)"
    assert len(fake_llm.calls) == 1
    assert "AgentOS 5 launched" in fake_llm.calls[0]["messages"][-1]["content"]


def test_synthesize_falls_back_to_raw_headlines_when_the_llm_fails(fake_llm, monkeypatch):
    from core.llm import LLMError

    def _raise(*a, **k):
        raise LLMError("down")

    monkeypatch.setattr(fake_llm, "_post", _raise)
    skill = WorldNewsSkill(llm=fake_llm)
    headlines = [Headline(category="world", source="BBC", title="Something happened",
                          url="https://x.test/1", published_at=time.time())]

    text = skill._synthesize(headlines)

    assert "Something happened" in text
    assert "BBC" in text


# ------------------------------------------------------------------ whats_up (full flow)
def _empty_fetcher() -> _FakeFetcher:
    return _FakeFetcher({})


def test_whats_up_pushes_the_digest_and_returns_ok_when_something_came_in(fake_llm):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Budget announced", "https://x.test/1", _rfc822(now))])),
    })
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, clock=lambda: now,
                           notify=lambda text: notified.append(text) or True)

    result = skill.whats_up(categories="kenya")

    assert result.ok is True
    assert result.data["categories"] == ["kenya"]
    assert result.data["headline_count"] == 1
    assert len(notified) == 1
    assert "Kenya" in notified[0]


def test_whats_up_with_an_unknown_category_falls_back_to_everything(fake_llm):
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(), notify=lambda t: True)
    result = skill.whats_up(categories="not_a_real_category")
    assert set(result.data["categories"]) == set(FEEDS.keys())


def test_whats_up_reports_ok_false_when_every_source_is_unreachable(fake_llm):
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(),
                           notify=lambda text: notified.append(text) or True)

    result = skill.whats_up()

    assert result.ok is False
    assert result.data["headline_count"] == 0
    assert "couldn't reach" in notified[0].lower()


def test_whats_up_never_calls_notify_when_notify_is_false(fake_llm):
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(),
                           notify=lambda text: notified.append(text) or True)

    skill.whats_up(notify=False)

    assert notified == []


def test_scheduled_jobs_registers_a_daily_cron():
    skill = WorldNewsSkill()
    jobs = skill.scheduled_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "world_news.briefing"
    assert jobs[0].trigger == "cron"
