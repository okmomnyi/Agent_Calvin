"""World-news briefing skill: RSS parsing, per-source degrade, freshness window, synthesis
fallback, and injectable notify (nothing in this suite may reach a real Telegram/RSS feed)."""

from __future__ import annotations

import time

from skills.world_news import FEEDS, Cluster, Headline, WorldNewsSkill, _cluster_headlines, _parse_rss

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


# ------------------------------------------------------------------ dedup (S1)
class _FakeEmbedder:
    """Fixture vectors keyed by title -- full control over similarity, no real model needed
    (per the S1 spec: "canned RSS + a fake embedder with fixture vectors")."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[t] for t in texts]

    def embed(self, text: str) -> list[float]:
        return self._vectors[text]


class _BrokenEmbedder:
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")


def _headline(source: str, title: str, category: str = "world", group: str = "") -> Headline:
    return Headline(category=category, source=source, title=title,
                    url=f"https://x.test/{source}/{title[:8]}", published_at=time.time(),
                    group=group)


def test_ten_headlines_about_one_event_collapse_into_one_cluster():
    titles = [
        "Ceasefire announced after weeks of talks",
        "Truce declared in long-running conflict",
        "Peace deal reached, officials confirm",
        "Warring sides agree to halt hostilities",
        "Ceasefire takes effect at midnight",
        "Officials confirm ceasefire agreement",
        "Fighting stops as truce begins",
        "Both sides sign ceasefire accord",
        "Conflict paused under new truce",
        "Ceasefire holds on first day",
    ]
    sources = ["BBC World", "Al Jazeera", "Reuters", "AP"]
    headlines = [_headline(sources[i % len(sources)], t) for i, t in enumerate(titles)]
    # Same event -> identical fixture vector for every title (full control; no real model).
    embedder = _FakeEmbedder({t: [1.0, 0.0] for t in titles})

    clusters = _cluster_headlines(headlines, embedder, threshold=0.80)

    assert len(clusters) == 1
    assert len(clusters[0].members) == 10
    assert set(clusters[0].sources) == set(sources)


def test_two_different_stories_sharing_a_word_do_not_merge():
    headlines = [
        _headline("BBC World", "Fire breaks out at downtown factory"),
        _headline("Al Jazeera", "Factory workers strike over pay dispute"),
    ]
    embedder = _FakeEmbedder({
        "Fire breaks out at downtown factory": [1.0, 0.0],
        "Factory workers strike over pay dispute": [0.0, 1.0],  # orthogonal: unrelated
    })

    clusters = _cluster_headlines(headlines, embedder, threshold=0.80)

    assert len(clusters) == 2


def test_cluster_headlines_with_no_input_returns_no_clusters():
    assert _cluster_headlines([], _FakeEmbedder({}), threshold=0.80) == []


def test_skill_cluster_falls_back_to_one_story_per_headline_when_the_embedder_fails():
    """The embedder-down path: never go silent, degrade to today's un-deduped behaviour."""
    skill = WorldNewsSkill(embedder=_BrokenEmbedder())
    headlines = [_headline("BBC World", "Something happened"),
                _headline("Al Jazeera", "Something else entirely")]

    clusters = skill._cluster(headlines)

    assert len(clusters) == 2
    assert clusters[0].members == [headlines[0]]
    assert clusters[1].members == [headlines[1]]


def test_skill_cluster_reads_the_configured_dedup_threshold(monkeypatch):
    calls: list[tuple] = []

    class _FakeSettings:
        def get(self, *keys, default=None):
            calls.append(keys)
            return default

    monkeypatch.setattr("skills.world_news.get_settings", lambda: _FakeSettings())
    skill = WorldNewsSkill(embedder=_FakeEmbedder({"A": [1.0, 0.0]}))

    skill._cluster([_headline("BBC World", "A")])

    assert ("world_news", "dedup_threshold") in calls


# ------------------------------------------------------------------ synthesis
def test_synthesize_with_no_headlines_says_so_without_calling_the_llm(fake_llm):
    skill = WorldNewsSkill(llm=fake_llm)
    text = skill._synthesize([])
    assert "nothing fresh" in text.lower()
    assert fake_llm.calls == []


def test_synthesize_calls_the_llm_with_the_headlines_as_context(fake_llm):
    fake_llm.post_result = "AgentOS 5 launched today. (TechCrunch)"
    skill = WorldNewsSkill(llm=fake_llm)
    cluster = Cluster(category="tech_ai", members=[
        Headline(category="tech_ai", source="TechCrunch", title="AgentOS 5 launched",
                url="https://x.test/1", published_at=time.time())])

    text = skill._synthesize([cluster])

    assert text == "AgentOS 5 launched today. (TechCrunch)"
    assert len(fake_llm.calls) == 1
    assert "AgentOS 5 launched" in fake_llm.calls[0]["messages"][-1]["content"]


def test_synthesize_cites_every_source_in_a_merged_cluster(fake_llm):
    fake_llm.post_result = "A ceasefire was announced. (BBC World, Al Jazeera)"
    skill = WorldNewsSkill(llm=fake_llm)
    cluster = Cluster(category="world", members=[
        Headline(category="world", source="BBC World", title="Ceasefire announced",
                url="https://x.test/1", published_at=time.time()),
        Headline(category="world", source="Al Jazeera", title="Truce declared",
                url="https://x.test/2", published_at=time.time()),
    ])

    prompt = skill._synthesize([cluster])
    sent_context = fake_llm.calls[0]["messages"][-1]["content"]

    assert "BBC World" in sent_context and "Al Jazeera" in sent_context
    assert "sources: BBC World, Al Jazeera" in sent_context
    assert "BBC World" in prompt and "Al Jazeera" in prompt


def test_synthesize_falls_back_to_raw_stories_when_the_llm_fails(fake_llm, monkeypatch):
    from core.llm import LLMError

    def _raise(*a, **k):
        raise LLMError("down")

    monkeypatch.setattr(fake_llm, "_post", _raise)
    skill = WorldNewsSkill(llm=fake_llm)
    cluster = Cluster(category="world", members=[
        Headline(category="world", source="BBC", title="Something happened",
                url="https://x.test/1", published_at=time.time())])

    text = skill._synthesize([cluster])

    assert "Something happened" in text
    assert "BBC" in text


# ------------------------------------------------------------------ whats_up (full flow)
def _empty_fetcher() -> _FakeFetcher:
    return _FakeFetcher({})


def test_whats_up_pushes_the_digest_and_returns_ok_when_something_came_in(fake_llm, mem):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Budget announced", "https://x.test/1", _rfc822(now))])),
    })
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=mem, clock=lambda: now,
                           notify=lambda text: notified.append(text) or True)

    result = skill.whats_up(categories="kenya")

    assert result.ok is True
    assert result.data["categories"] == ["kenya"]
    assert result.data["headline_count"] == 1
    assert len(notified) == 1
    assert "Kenya" in notified[0]


def test_whats_up_with_an_unknown_category_falls_back_to_everything(fake_llm, mem):
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(), memory=mem, notify=lambda t: True)
    result = skill.whats_up(categories="not_a_real_category")
    assert set(result.data["categories"]) == set(FEEDS.keys())


def test_whats_up_reports_ok_false_when_every_source_is_unreachable(fake_llm, mem):
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(), memory=mem,
                           notify=lambda text: notified.append(text) or True)

    result = skill.whats_up()

    assert result.ok is False
    assert result.data["headline_count"] == 0
    assert "couldn't reach" in notified[0].lower()


def test_whats_up_never_calls_notify_when_notify_is_false(fake_llm, mem):
    notified: list[str] = []
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(), memory=mem,
                           notify=lambda text: notified.append(text) or True)

    skill.whats_up(notify=False)

    assert notified == []


def test_scheduled_jobs_registers_a_daily_cron():
    skill = WorldNewsSkill()
    jobs = skill.scheduled_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "world_news.briefing"
    assert jobs[0].trigger == "cron"


# ------------------------------------------------------------------ "since last asked" (S2)
class _BrokenMem:
    """Simulates a DB outage -- every call raises, proving whats_up() degrades to showing
    everything rather than crashing or going silent."""

    def execute(self, *a, **k):
        raise RuntimeError("db down")

    def tx(self):
        raise RuntimeError("db down")


def test_watermark_is_none_with_nothing_delivered_yet(mem):
    skill = WorldNewsSkill(memory=mem)
    assert skill._watermark("kenya") is None


def test_watermark_reflects_the_latest_delivery(mem):
    skill = WorldNewsSkill(memory=mem, clock=lambda: 5_000_000.0)
    skill._record_delivered("kenya", ["https://x.test/1"])
    assert skill._watermark("kenya") == 5_000_000.0


def test_record_delivered_upserts_rather_than_duplicating(mem):
    skill = WorldNewsSkill(memory=mem, clock=lambda: 1_000_000.0)
    skill._record_delivered("kenya", ["https://x.test/1"])
    skill._record_delivered("kenya", ["https://x.test/1"])

    row = mem.execute("SELECT COUNT(*) c FROM world_news_delivered WHERE category=%s AND url=%s",
                      ("kenya", "https://x.test/1")).fetchone()
    assert row["c"] == 1


def test_retire_stale_delivered_excludes_old_rows_from_the_delivered_set(mem):
    now = [1_000_000.0]
    skill = WorldNewsSkill(memory=mem, clock=lambda: now[0])
    skill._record_delivered("kenya", ["https://x.test/old"])

    now[0] += 4 * 86400  # past the 3-day default retention
    skill._retire_stale_delivered()

    assert skill._delivered_urls("kenya") == set()


def test_second_call_within_the_window_shows_only_new_clusters(fake_llm, mem):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Budget passed", "https://x.test/budget", _rfc822(now))])),
    })
    skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=mem, clock=lambda: now,
                           notify=lambda t: True)

    first = skill.whats_up(categories="kenya")
    assert first.data["new_story_count"] == 1

    second = skill.whats_up(categories="kenya")
    assert second.data["new_story_count"] == 0
    assert "Nothing new since your last check" in second.text

    # a genuinely new, lexically distinct story appears alongside the old one
    fetcher._by_url[urls[0][1]] = _FakeResponse(200, _rss([
        ("Budget passed", "https://x.test/budget", _rfc822(now)),
        ("Opposition leader arrested overnight", "https://x.test/arrest", _rfc822(now)),
    ]))
    third = skill.whats_up(categories="kenya")
    assert third.data["new_story_count"] == 1


def test_scheduled_push_and_on_demand_share_one_watermark_across_instances(fake_llm, mem):
    """Two separate WorldNewsSkill instances (as the cron-triggered call and a later
    on-demand request would be) must read/advance the SAME state -- proving there is one
    source of truth, not two independent watermarks that could drift."""
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Story", "https://x.test/1", _rfc822(now))])),
    })

    push_skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=mem, clock=lambda: now,
                                notify=lambda t: True)
    push_result = push_skill.whats_up()  # simulates the cron firing with no explicit args

    ondemand_skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=mem, clock=lambda: now,
                                    notify=lambda t: True)
    ondemand_result = ondemand_skill.whats_up(categories="kenya")

    assert push_result.data["new_story_count"] == 1
    assert ondemand_result.data["new_story_count"] == 0
    assert push_skill._watermark("kenya") == ondemand_skill._watermark("kenya") == now


def test_full_flag_bypasses_the_delivered_filter(fake_llm, mem):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Story", "https://x.test/1", _rfc822(now))])),
    })
    skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=mem, clock=lambda: now,
                           notify=lambda t: True)

    skill.whats_up(categories="kenya")
    result = skill.whats_up(categories="kenya", full=True)

    assert result.data["new_story_count"] == 1
    assert result.data["full"] is True
    assert "full 24h window" in result.text


def test_full_keyword_in_categories_text_triggers_full_mode(fake_llm, mem):
    skill = WorldNewsSkill(llm=fake_llm, fetcher=_empty_fetcher(), memory=mem, notify=lambda t: True)

    result = skill.whats_up(categories="kenya full")

    assert result.data["full"] is True
    assert result.data["categories"] == ["kenya"]


def test_whats_up_degrades_to_showing_everything_when_delivered_state_is_unreadable(fake_llm):
    now = 2_000_000.0
    urls = FEEDS["kenya"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("Story", "https://x.test/1", _rfc822(now))])),
    })
    skill = WorldNewsSkill(llm=fake_llm, fetcher=fetcher, memory=_BrokenMem(), clock=lambda: now,
                           notify=lambda t: True)

    result = skill.whats_up(categories="kenya")

    assert result.ok is True
    assert result.data["new_story_count"] == 1  # degraded to "show everything", not a crash/silent drop


# ------------------------------------------------------------------ independence groups (S3)
def test_all_feeds_have_a_non_empty_independence_group():
    for category, sources in FEEDS.items():
        for src in sources:
            assert src.group, f"{src.name} in {category!r} has no independence group set"


def test_fetch_category_stamps_the_source_group_onto_headlines():
    now = 2_000_000.0
    urls = FEEDS["world"]
    fetcher = _FakeFetcher({
        urls[0][1]: _FakeResponse(200, _rss([("BBC story", "https://x.test/bbc", _rfc822(now))])),
    })
    skill = WorldNewsSkill(fetcher=fetcher, clock=lambda: now)

    headlines = skill._fetch_category("world")

    assert headlines[0].group == urls[0].group == "bbc"


def test_cluster_with_only_bbc_group_members_has_corroboration_one():
    """Two feeds, one publisher (BBC World + BBC Technology) -- one independent source."""
    cluster = Cluster(category="world", members=[
        _headline("BBC World", "Story", group="bbc"),
        _headline("BBC Technology", "Story variant", group="bbc"),
    ])
    assert cluster.independent_groups == {"bbc"}
    assert cluster.corroboration == 1


def test_cluster_spanning_three_independent_groups_has_corroboration_three():
    """BBC + Guardian + UN News -- three genuinely separate editorial voices on one story
    (Reuters/AP substituted per the S3 stop-gate: neither has a working free RSS feed)."""
    cluster = Cluster(category="world", members=[
        _headline("BBC World", "Story", group="bbc"),
        _headline("The Guardian", "Story", group="guardian"),
        _headline("UN News", "Story", group="un"),
    ])
    assert cluster.corroboration == 3


def test_nation_africa_and_business_daily_africa_share_one_independence_group():
    """The specific source-monoculture case S3 exists to close: two Kenyan outlets, one
    publisher (Nation Media Group)."""
    groups = {s.name: s.group for s in FEEDS["kenya"]}
    assert groups["Nation Africa"] == groups["Business Daily Africa"]
    assert groups["Nation Africa"] != groups["The Standard"]  # a genuinely different publisher


def test_a_same_publisher_cluster_does_not_inflate_corroboration():
    cluster = Cluster(category="kenya", members=[
        _headline("Nation Africa", "Budget story A", group="nation_media_group"),
        _headline("Nation Africa", "Budget story B", group="nation_media_group"),
        _headline("Business Daily Africa", "Budget story C", group="nation_media_group"),
    ])
    assert cluster.corroboration == 1, "3 headlines, ONE publisher -- must not read as 3 independent sources"
