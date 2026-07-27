"""World-news awareness skill (Phase 37, dedup added in 37b).

A daily "what's up today" briefing across world & conflicts, AI/tech, sports, business, and
Kenya-specific news, plus the same content on demand ("what's up today", /news). Free RSS
feeds only (no API key — §0 free-first), synthesized with the same discipline research.py
already enforces: every line in the digest traces back to an actual fetched headline, and a
category with nothing fresh in the window says so plainly rather than being padded out.

Phase 37b/S1: headlines are clustered into stories by embedding similarity (core.embeddings,
the same NIM-hosted embedder helper semantic.py uses for persona/CV recall) before synthesis, so two wires
covering one event collapse into one cited line instead of two separate ones. Clustering is
in-memory and per-run — nothing here writes to semantic_index, which is a closed namespace
for durable facts/notes/docs, not a day's transient headlines.

Phase 37b/S2: a persisted "delivered" set (world_news_delivered, per category) means asking
again mid-morning only surfaces genuinely new stories instead of replaying the whole 24h
window — the scheduled 07:30 push and the on-demand path read and advance the SAME state, so
they can never drift onto two different ideas of "since last time". Pass full=True (or the
word "full" in the categories text, e.g. "/news full") to force the whole window regardless.

Phase 37b/S3: every source carries an independence GROUP (shared ownership/editorial
control) alongside its (name, url) — Nation Africa and Business Daily Africa both belong to
Nation Media Group, all BBC feeds share one group, and so on. Cluster.corroboration counts
DISTINCT groups, not raw headline count, so two feeds from one publisher can never masquerade
as two independent sources agreeing on a story. This is what S5's breaking-news bar checks
against. Sources were widened beyond BBC/Al Jazeera (The Guardian, UN News, AllAfrica, arXiv
cs.AI, OpenAI's blog) — Reuters and AP were tried first but neither has a working free RSS
feed left, so they're not in the list.

Phase 37b/S4: a cross-category "front page" leads the brief -- the top 3 stories across ALL
requested categories, picked by a fully deterministic score (magnitude x corroboration x
configured interest relevance), not by the model. The model only writes the prose for
whatever the score already chose; ranking never varies run-to-run for the same clusters.
Only shown when spanning more than one category -- "/news kenya" alone doesn't need a front
page on top of itself.

Phase 37b/S5: a lightweight, more-frequent poll (check_breaking, separate from the daily
digest) fires AT MOST ONE push, and only when a story clears every bar: high independent
corroboration (S3's groups, not raw headline count), tightly time-clustered coverage, never
already pushed before for this story-lineage, and a GLOBAL cooldown has elapsed. There is no
keyword path anywhere in this -- a headline containing "breaking"/"war"/"urgent" from a
single source never fires. This is a rare, high-bar exception to pull-only delivery, not a
second firehose (AGENTOS_BUG_LIST.md #17's own lesson: an ungated stream drowns the person
it's meant to help).

Phase 37b/S6: categories and sources moved from a hardcoded Python dict to config.yaml's
world_news.sources -- _feeds() reads it live every call (same convention as every other
tunable in this skill), falling back to DEFAULT_FEEDS if that section is missing or
malformed. Adding, removing, or re-grouping a source is now a config edit, not a code change.

Deliberately NOT market prediction or financial advice — see skills/markets.py (Phase 38)
for real price data correlated with news (via recent_headlines() below); nothing here
forecasts or advises, and neither does that skill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, NamedTuple
from xml.etree import ElementTree as ET

from core.config import get_settings
from core.embeddings import cosine
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.stage import ArticlesWidget, FactWidget, StageDirective, get_stage_bus

log = get_logger("skills.world_news")

class Source(NamedTuple):
    """A NamedTuple, not a plain dataclass, so `source[1]` still means "the URL" for any
    code that indexes it positionally -- same shape as the old (name, url) tuples, `group`
    just extends it. `group` is the INDEPENDENCE tag (Phase 37b/S3): sources sharing real
    editorial/ownership control share a group, so two feeds from one publisher never count
    as two independent corroborating sources for a story (see Cluster.corroboration)."""
    name: str
    url: str
    group: str


# category -> [Source(name, url, group), ...]. All free, no API key, verified live (a dead
# feed degrades to "nothing fresh from this source" for that one run, never fails the
# briefing). Reuters and AP were tried first (per the original ask) and dropped: Reuters no
# longer publishes free public RSS at all (feeds.reuters.com is dead), and every AP RSS URL
# tried returns the plain HTML site, not a feed -- neither offers a free, ToS-clean feed to
# wire in. The Guardian and UN News substitute as editorially-independent world coverage.
#
# This is the FALLBACK (Phase 37b/S6): production reads _feeds() live from config.yaml's
# world_news.sources every call, so adding/removing/re-grouping a source is a config edit,
# not a code change. DEFAULT_FEEDS is what a fresh/un-updated config.yaml falls back to, and
# what tests build fixtures against directly (they don't need to exercise the config-reading
# path itself unless that's specifically what they're testing).
DEFAULT_FEEDS: dict[str, list[Source]] = {
    "world": [
        Source("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "bbc"),
        Source("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "aljazeera"),
        Source("The Guardian", "https://www.theguardian.com/world/rss", "guardian"),
        Source("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "un"),
    ],
    "tech_ai": [
        Source("TechCrunch", "https://techcrunch.com/feed/", "techcrunch"),
        Source("BBC Technology", "http://feeds.bbci.co.uk/news/technology/rss.xml", "bbc"),
        # A dedicated AI-releases source instead of leaning entirely on TechCrunch's general
        # tech firehose: arXiv cs.AI catches research/model releases days before mainstream
        # tech press covers them; OpenAI's own blog catches product announcements directly.
        Source("arXiv cs.AI", "http://export.arxiv.org/rss/cs.AI", "arxiv"),
        Source("OpenAI News", "https://openai.com/news/rss.xml", "openai"),
    ],
    "sports": [
        Source("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml", "bbc"),
        Source("ESPN", "https://www.espn.com/espn/rss/news", "espn"),
    ],
    "business": [
        Source("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml", "bbc"),
    ],
    "kenya": [
        # Nation Africa and Business Daily Africa are BOTH published by Nation Media Group --
        # the same independence group. Without this tag they'd count as two corroborating
        # sources on a story that really has only one editorial voice behind it; this is
        # exactly the "source monoculture gaming the novelty/corroboration detector" case
        # S3 exists to close.
        Source("Nation Africa", "https://nation.africa/kenya/rss.xml", "nation_media_group"),
        Source("Business Daily Africa", "https://www.businessdailyafrica.com/bd/rss.xml",
               "nation_media_group"),
        Source("The Standard", "https://www.standardmedia.co.ke/rss/headlines.php", "standard_group"),
        # Continental breadth beyond Kenya-only coverage, per the original ask.
        Source("AllAfrica", "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf", "allafrica"),
    ],
}


def _feeds() -> dict[str, list[Source]]:
    """The live source list (Phase 37b/S6) -- read fresh from config.yaml's
    world_news.sources on every call (same convention as dedup_threshold/interest_order/etc:
    nothing in this skill caches a config value across calls). Falls back to DEFAULT_FEEDS
    if that section is missing (a fresh/un-updated config.yaml) or malformed -- a typo'd
    config must degrade to a known-good source list, not take the whole skill down.
    """
    raw = get_settings().get("world_news", "sources", default=None)
    if not raw:
        return DEFAULT_FEEDS
    try:
        return {
            category: [Source(s["name"], s["url"], s["group"]) for s in sources]
            for category, sources in raw.items()
        }
    except (KeyError, TypeError, AttributeError):
        log.warning("world_news: config.yaml's world_news.sources is malformed, using defaults")
        return DEFAULT_FEEDS


CATEGORY_LABEL = {
    "world": "🌍 World & Conflicts",
    "tech_ai": "🤖 AI & Tech",
    "sports": "🏆 Sports",
    "business": "💼 Business",
    "kenya": "🇰🇪 Kenya",
}

_WINDOW_SECONDS = 24 * 3600  # only items from roughly the last day count as "today"
_MAX_STORIES_TO_MODEL = 15  # a category can return 40+ headlines; the newest handful of STORIES is plenty
# Cosine threshold above which two headlines are treated as the same story. NIM embedding
# output is normalized, so this sits in the "same event, different wording" band rather than "same
# topic" -- deliberately conservative (merging two distinct stories is the worse failure: it
# would let one source's framing stand in for another's). Config-overridable since the right
# value benefits from tuning against real production output, not guessed once and frozen.
_DEDUP_THRESHOLD_DEFAULT = 0.80
# How long a "delivered" record blocks a URL from being shown again as "new" before it's
# retired (soft — never deleted, §0 P4). Generous relative to the 24h freshness window: a
# slow-developing story shouldn't reappear as "new" just because it's been a few days.
_DELIVERED_RETENTION_DAYS_DEFAULT = 3
# Ranked interest order for the front page (Phase 37b/S4), highest first. Maps the original
# ask (world/conflict > AI > markets > Kenya > sport) onto this skill's actual category keys
# -- "markets" isn't its own category yet (skills/markets.py is a separate, later piece), so
# "business" stands in for it. Config-overridable (world_news.interest_order).
_INTEREST_ORDER_DEFAULT = ["world", "tech_ai", "business", "kenya", "sports"]
_FRONT_PAGE_SIZE = 3

# Phase 38: spoken/typed topic words -> this skill's own category keys, so "show me what's
# happening with AI" or "the conflict" resolves without a new taxonomy. Deliberately a small,
# literal alias table (not fuzzy/LLM matching) -- display() must stay fast and deterministic
# since it can run on every stage subject change, and an unresolvable topic should say so
# (return None) rather than guess.
_TOPIC_ALIASES = {
    "conflict": "world", "war": "world", "geopolitics": "world", "global": "world",
    "ai": "tech_ai", "tech": "tech_ai", "technology": "tech_ai",
    "sport": "sports",
    "markets": "business", "economy": "business", "economic": "business",
    "kenyan": "kenya",
}
_MAX_DISPLAY_ITEMS = 6

# Breaking news (Phase 37b/S5) -- deliberately conservative defaults. This is a RARE, high-bar
# exception to pull-only delivery, not a second firehose (AGENTOS_BUG_LIST.md #17's own lesson:
# an unbounded stream drowns the person it's meant to help). Corroboration-gated, never
# keyword-gated -- a headline containing "breaking"/"war"/"urgent" from ONE source never fires.
_BREAKING_CORROBORATION_BAR_DEFAULT = 3    # distinct independent GROUPS, not raw headlines
_BREAKING_WINDOW_HOURS_DEFAULT = 6         # how tightly-clustered in TIME the coverage must be
_BREAKING_COOLDOWN_MINUTES_DEFAULT = 60    # GLOBAL, system-wide -- not per category
_BREAKING_POLL_MINUTES_DEFAULT = 20        # more frequent than the daily digest, still modest
_BREAKING_RETENTION_DAYS_DEFAULT = 7


@dataclass
class Headline:
    category: str
    source: str
    title: str
    url: str
    published_at: float | None = None
    # Independence group of the source that reported it (Phase 37b/S3), e.g. "bbc" or
    # "nation_media_group". Defaults to "" for any caller that doesn't set it (tests
    # constructing a Headline directly); production always stamps a real group from _feeds().
    group: str = ""
    # Feed thumbnail URL (Phase 38), or None. NEVER populated by an image search — only
    # what the RSS item itself carries (media:thumbnail / media:content / enclosure). A
    # feed with no image for this item leaves this None; the stage's typed fallback card
    # handles that, never a broken <img> and never a substituted picture from anywhere else.
    image: str | None = None


@dataclass
class Cluster:
    """One story. `members` holds every (source, title, link) reporting it -- synthesis
    cites all of them; nothing here merges their TEXT, only the fact that they co-occur."""
    category: str
    members: list[Headline]

    @property
    def independent_groups(self) -> set[str]:
        """Distinct independence groups among this story's sources (Phase 37b/S3) -- two
        headlines from the SAME group (e.g. two BBC feeds, or Nation Africa + Business Daily
        Africa) count once. This is what S5's breaking-news corroboration bar checks against,
        not the raw member count."""
        return {h.group for h in self.members}

    @property
    def corroboration(self) -> int:
        """How many INDEPENDENT sources are behind this story. A cluster with 5 headlines
        all from one publisher's group has corroboration 1, not 5."""
        return len(self.independent_groups)

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for h in self.members:
            if h.source not in seen:
                seen.append(h.source)
        return seen

    @property
    def publish_span_hours(self) -> float:
        """How tightly-clustered in TIME this story's coverage is (Phase 37b/S5) -- 3
        independent sources publishing within the same hour reads very differently from 3
        sources publishing across 20 hours, even though corroboration is identical. Undated
        or single-dated-point clusters return 0.0 (never penalized for missing dates)."""
        dated = [h.published_at for h in self.members if h.published_at is not None]
        if len(dated) < 2:
            return 0.0
        return (max(dated) - min(dated)) / 3600


def _cluster_headlines(headlines: list[Headline], embedder: Any, threshold: float) -> list[Cluster]:
    """Greedy single-pass clustering against a running centroid per cluster -- O(n * clusters),
    fine at the scale of a few dozen headlines per category. Embeddings over title-string
    matching on purpose (per the project's own semantic.py precedent): it catches the same
    event worded differently ("PM announces reforms" / "Prime minister unveils economic
    plan") and, just as importantly, does NOT merge two different stories that merely share
    a word ("factory fire" / "factory strike").
    """
    if not headlines:
        return []
    vectors = embedder.embed_batch([h.title for h in headlines])
    clusters: list[Cluster] = []
    centroids: list[list[float]] = []
    for headline, vec in zip(headlines, vectors):
        best_idx, best_score = None, 0.0
        for i, centroid in enumerate(centroids):
            score = cosine(vec, centroid)
            if score > best_score:
                best_score, best_idx = score, i
        if best_idx is not None and best_score >= threshold:
            clusters[best_idx].members.append(headline)
            n = len(clusters[best_idx].members)
            centroids[best_idx] = [(c * (n - 1) + v) / n for c, v in zip(centroids[best_idx], vec)]
        else:
            clusters.append(Cluster(category=headline.category, members=[headline]))
            centroids.append(vec)
    return clusters


def _local_tag(tag: str) -> str:
    """Strip ElementTree's `{namespace-uri}` prefix so a tag can be matched by its bare
    name regardless of which namespace prefix (or none) the feed declared it under."""
    return tag.rsplit("}", 1)[-1]


def _extract_image(item: ET.Element) -> str | None:
    """A feed thumbnail URL, straight from the RSS item — media:thumbnail, an image
    media:content, or an image enclosure, in that preference order. Returns None if the
    item carries none of these; this is the ONLY source of `Headline.image` anywhere in
    this codebase (Phase 38's hard content-safety line: never an image search, ever)."""
    for child in item.iter():
        if child is item:
            continue
        local = _local_tag(child.tag)
        url = (child.get("url") or "").strip()
        if not url:
            continue
        if local == "thumbnail":
            return url
        if local == "content" and (child.get("medium") == "image"
                                   or (child.get("type") or "").startswith("image/")):
            return url
        if local == "enclosure" and (child.get("type") or "").startswith("image/"):
            return url
    return None


def _parse_rss(xml_text: str, source: str, category: str, group: str = "") -> list[Headline]:
    """RSS 2.0 <item> parsing via stdlib ElementTree — no new dependency for something this
    simple. Malformed XML degrades to an empty list rather than raising."""
    out: list[Headline] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.warning("world_news: %s (%s) returned unparseable XML", source, category)
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        published_at = None
        pub_raw = item.findtext("pubDate") or ""
        if pub_raw:
            try:
                published_at = parsedate_to_datetime(pub_raw).timestamp()
            except (ValueError, TypeError):
                published_at = None
        out.append(Headline(category=category, source=source, title=title, url=link,
                            published_at=published_at, group=group,
                            image=_extract_image(item)))
    return out


class WorldNewsSkill(BaseSkill):
    name = "world_news"
    acks = {
        "whats_up": ("On it — scanning the wires now…", "moment"),
        "news": ("On it — scanning the wires now…", "moment"),
    }

    def __init__(self, llm: LLMClient | None = None, fetcher: Any | None = None,
                 embedder: Any | None = None, memory: Memory | None = None,
                 notify: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._llm = llm
        self._fetcher = fetcher
        self._embedder = embedder
        self._mem = memory
        # Injectable, same as every other skill that can reach Calvin's phone — a test must
        # be able to replace this, or the suite texts him.
        self._notify = notify or send_telegram
        self._now = clock

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def fetcher(self):
        if self._fetcher is None:
            from skills.job_hunter.fetcher import Fetcher

            # RSS feeds are published specifically to be polled — respect_robots stays off
            # the same way research.py's DuckDuckGoSearcher treats a search endpoint. This
            # exception is scoped to RSS-feed URLs only: if this skill ever fetches an
            # ARTICLE page (a future "read more"), that fetch must consult robots normally.
            self._fetcher = Fetcher(respect_robots=False)
        return self._fetcher

    @property
    def embedder(self):
        if self._embedder is None:
            from core.embeddings import get_embedder

            # Same helper semantic.py uses for persona/CV recall (NIM embedding model ->
            # sentence-transformers -> hashing). Used here purely in-memory for same-run headline
            # clustering -- not persisted to semantic_index, which is a closed namespace
            # (fact/note/doc) for durable recall, not a day's transient headlines.
            self._embedder = get_embedder("auto")
        return self._embedder

    def contract(self) -> SkillContract:
        """Reads only tone/general — how the digest is written, never what it may report.
        `never_invents_a_source` is non-negotiable: a line in the briefing traces to an
        actually-fetched headline, or the category says nothing fresh came in."""
        return SkillContract(reads_categories=["tone", "general"],
                             hard_invariants=["never_invents_a_source"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"whats_up": self.whats_up, "news": self.whats_up}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        hour = int(get_settings().get("world_news", "briefing_hour", default=7))
        minute = int(get_settings().get("world_news", "briefing_minute", default=30))
        poll_minutes = int(get_settings().get(
            "world_news", "breaking_poll_minutes", default=_BREAKING_POLL_MINUTES_DEFAULT))
        return [
            ScheduledJob(id="world_news.briefing", func=self.whats_up, trigger="cron",
                        kwargs={"hour": hour, "minute": minute}),
            # Separate from the daily digest on purpose -- more frequent, but still gated hard
            # by check_breaking() itself (corroboration bar + global cooldown), not by how
            # often this timer fires. See the "breaking news (S5)" section above.
            ScheduledJob(id="world_news.breaking_check", func=self.check_breaking,
                        trigger="interval", kwargs={"minutes": poll_minutes}),
        ]

    def recent_headlines(self, category: str) -> list[Headline]:
        """Public accessor so other skills (skills/markets.py's news-correlation step) can
        reuse this skill's own feeds/freshness-window logic instead of duplicating RSS
        fetching -- one place owns "what's in the news right now"."""
        return self._fetch_category(category)

    # ------------------------------------------------------------- stage widget (Phase 38)
    def _resolve_topic(self, topic: str) -> str | None:
        """A spoken/typed topic -> one of this skill's own category keys, or None if it
        can't be resolved -- the caller must omit the widget rather than guess a category.
        Blank topic defaults to the top of the configured interest order (the same "what
        matters most" ranking S4's front page uses)."""
        feeds = _feeds()
        t = (topic or "").strip().lower()
        if not t:
            order = get_settings().get("world_news", "interest_order",
                                       default=_INTEREST_ORDER_DEFAULT)
            return order[0] if order else None
        if t in feeds:
            return t
        for alias, category in _TOPIC_ALIASES.items():
            if alias in t and category in feeds:
                return category
        return None

    @staticmethod
    def _cluster_image(cluster: Cluster) -> str | None:
        for h in cluster.members:
            if h.image:
                return h.image
        return None

    def _cluster_items(self, clusters: list[Cluster], max_items: int) -> list[dict[str, Any]]:
        items = []
        for cluster in clusters[:max_items]:
            lead = cluster.members[0]
            items.append({
                "title": lead.title,
                "source": ", ".join(cluster.sources),
                "url": lead.url,
                "published": lead.published_at if lead.published_at is not None else self._now(),
                "image": self._cluster_image(cluster),
            })
        return items

    def display(self, topic: str = "", max_items: int = _MAX_DISPLAY_ITEMS,
                **_: Any) -> ArticlesWidget | None:
        """Presenter-facing (core/presenter.py): an articles widget for `topic`, built from
        this skill's own fetch+cluster pipeline -- no LLM call (display() must be fast
        enough to run on every stage subject change) and no new data source. Returns None
        -- never a fabricated/empty card -- when the topic doesn't resolve to a known
        category or nothing was fetchable; the presenter omits the widget entirely, per
        Phase 38's "un-buildable widget -> omitted, never faked" rule."""
        category = self._resolve_topic(topic)
        if category is None:
            return None
        try:
            headlines = self._fetch_category(category)
        except Exception:  # noqa: BLE001 - display() degrades to "omit", never raises
            log.warning("world_news: display() fetch failed for %s", category, exc_info=True)
            return None
        if not headlines:
            return None
        clusters = self._cluster(headlines)
        items = self._cluster_items(clusters, max_items)
        if not items:
            return None
        return ArticlesWidget(topic=CATEGORY_LABEL.get(category, category), items=items)

    # ------------------------------------------------------------- fetch
    def _fetch_category(self, category: str) -> list[Headline]:
        cutoff = self._now() - _WINDOW_SECONDS
        out: list[Headline] = []
        for src in _feeds().get(category, []):
            resp = self.fetcher.get(src.url, accept="application/rss+xml, text/xml")
            if resp is None or resp.status_code != 200:
                log.warning("world_news: %s (%s) unreachable, skipping this run", src.name, category)
                continue
            items = _parse_rss(resp.text, src.name, category, src.group)
            # Undated items (a feed with no pubDate) are kept — there's no basis to exclude
            # them; dated items outside the window are dropped rather than shown as "today".
            out.extend(h for h in items if h.published_at is None or h.published_at >= cutoff)
        return out

    # ------------------------------------------------------------- dedup
    def _cluster(self, headlines: list[Headline]) -> list[Cluster]:
        if not headlines:
            return []
        threshold = float(get_settings().get("world_news", "dedup_threshold",
                                             default=_DEDUP_THRESHOLD_DEFAULT))
        try:
            return _cluster_headlines(headlines, self.embedder, threshold)
        except Exception:  # noqa: BLE001 - clustering must never take the briefing down
            log.warning("world_news: clustering failed, falling back to one story per headline")
            return [Cluster(category=h.category, members=[h]) for h in headlines]

    # ------------------------------------------------------------- "since last asked" (S2)
    # Novelty is tracked by URL membership, not a bare timestamp cutoff -- a story a slow
    # feed reports late must not be mistaken for something already seen just because it's
    # "older" than some watermark. The watermark itself (used only for the reply's own
    # wording, e.g. "since 07:30") is derived from this same table via _watermark(), so the
    # scheduled push and the on-demand path can never drift onto two different ideas of
    # "last time" -- there is exactly one source of truth.
    def _retire_stale_delivered(self) -> None:
        retention_days = float(get_settings().get(
            "world_news", "delivered_retention_days", default=_DELIVERED_RETENTION_DAYS_DEFAULT))
        cutoff = self._now() - retention_days * 86400
        with self.mem.tx() as conn:
            conn.execute(
                "UPDATE world_news_delivered SET retired_at=%s "
                "WHERE retired_at IS NULL AND delivered_at < %s",
                (self._now(), cutoff))

    def _delivered_urls(self, category: str) -> set[str]:
        rows = self.mem.execute(
            "SELECT url FROM world_news_delivered WHERE category=%s AND retired_at IS NULL",
            (category,)).fetchall()
        return {r["url"] for r in rows}

    def _watermark(self, category: str) -> float | None:
        row = self.mem.execute(
            "SELECT MAX(delivered_at) AS m FROM world_news_delivered "
            "WHERE category=%s AND retired_at IS NULL", (category,)).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def _record_delivered(self, category: str, urls: list[str]) -> None:
        if not urls:
            return
        now = self._now()
        with self.mem.tx() as conn:
            for url in urls:
                conn.execute(
                    "INSERT INTO world_news_delivered(category, url, delivered_at) "
                    "VALUES(%s,%s,%s) ON CONFLICT (category, url) "
                    "DO UPDATE SET delivered_at=EXCLUDED.delivered_at, retired_at=NULL",
                    (category, url, now))

    # ------------------------------------------------------------- front page (S4)
    # Ranking is entirely deterministic given its three inputs -- the model never picks the
    # order, only writes the prose for whatever _front_page() already chose. That keeps two
    # runs over the same clusters from ever disagreeing about what leads.
    def _score(self, cluster: Cluster, interest_order: list[str]) -> float:
        magnitude = len(cluster.members)        # raw volume of coverage feeding this story
        corroboration = cluster.corroboration    # DISTINCT independent sources (S3)
        try:
            relevance = len(interest_order) - interest_order.index(cluster.category)
        except ValueError:
            relevance = 0.5   # a category outside the configured order still shows, just last
        return magnitude * corroboration * relevance

    def _front_page(self, clusters: list[Cluster]) -> list[Cluster]:
        """Top stories across ALL categories, regardless of which one they came from --
        the "what's the single most important thing" answer. Only meaningful when spanning
        more than one category; a single-category ask doesn't need a front page on itself."""
        if not clusters:
            return []
        interest_order = get_settings().get(
            "world_news", "interest_order", default=_INTEREST_ORDER_DEFAULT)
        ranked = sorted(clusters, key=lambda c: self._score(c, interest_order), reverse=True)
        return ranked[:_FRONT_PAGE_SIZE]

    # ------------------------------------------------------------- breaking news (S5)
    # Corroboration-gated, NEVER keyword-gated (AGENTOS_BUG_LIST.md #17's own lesson applies
    # here too: an unbounded/ungated stream is how a feature meant to help drowns the person
    # it's for). A story only qualifies if independent wires -- not raw headlines, not one
    # loud source -- report it inside a tight time window. Even then it fires at most once
    # per story-lineage, and the cooldown is GLOBAL, not per-category, so a day with several
    # genuinely big stories still can't machine-gun.
    def _breaking_candidates(self, clusters: list[Cluster]) -> list[Cluster]:
        bar = int(get_settings().get(
            "world_news", "breaking_corroboration_bar", default=_BREAKING_CORROBORATION_BAR_DEFAULT))
        window_hours = float(get_settings().get(
            "world_news", "breaking_window_hours", default=_BREAKING_WINDOW_HOURS_DEFAULT))
        return [c for c in clusters
                if c.corroboration >= bar and c.publish_span_hours <= window_hours]

    def _already_breaking_pushed(self, category: str) -> set[str]:
        rows = self.mem.execute(
            "SELECT url FROM world_news_breaking_pushed WHERE category=%s AND retired_at IS NULL",
            (category,)).fetchall()
        return {r["url"] for r in rows}

    def _last_breaking_push_at(self) -> float | None:
        row = self.mem.execute(
            "SELECT MAX(pushed_at) AS m FROM world_news_breaking_pushed "
            "WHERE retired_at IS NULL").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def _record_breaking_pushed(self, category: str, urls: list[str]) -> None:
        if not urls:
            return
        now = self._now()
        with self.mem.tx() as conn:
            for url in urls:
                conn.execute(
                    "INSERT INTO world_news_breaking_pushed(category, url, pushed_at) "
                    "VALUES(%s,%s,%s) ON CONFLICT (category, url) "
                    "DO UPDATE SET pushed_at=EXCLUDED.pushed_at, retired_at=NULL",
                    (category, url, now))

    def _retire_stale_breaking_pushed(self) -> None:
        retention_days = float(get_settings().get(
            "world_news", "breaking_retention_days", default=_BREAKING_RETENTION_DAYS_DEFAULT))
        cutoff = self._now() - retention_days * 86400
        with self.mem.tx() as conn:
            conn.execute(
                "UPDATE world_news_breaking_pushed SET retired_at=%s "
                "WHERE retired_at IS NULL AND pushed_at < %s",
                (self._now(), cutoff))

    def _breaking_text(self, category: str, cluster: Cluster) -> str:
        prose = self._synthesize([cluster])
        return (f"🚨 BREAKING — {CATEGORY_LABEL.get(category, category)}\n"
                f"Corroborated by {cluster.corroboration} independent source(s): "
                f"{', '.join(cluster.sources)}\n\n{prose}")

    def check_breaking(self, **_: Any) -> CommandResult:
        """The lightweight poll (more frequent than the daily digest, still modest --
        world_news.breaking_poll_minutes). Fires AT MOST ONE push, and only when a story
        clears every bar: high independent corroboration, tightly time-clustered coverage,
        never already pushed before, and the global cooldown has elapsed. Any single failed
        check (DB read, one category's fetch) degrades that one thing, never the whole poll.
        """
        try:
            self._retire_stale_breaking_pushed()
        except Exception:  # noqa: BLE001 - housekeeping must never block the poll
            log.warning("world_news: retiring stale breaking-pushed rows failed", exc_info=True)

        cooldown_minutes = float(get_settings().get(
            "world_news", "breaking_cooldown_minutes", default=_BREAKING_COOLDOWN_MINUTES_DEFAULT))
        try:
            last_push = self._last_breaking_push_at()
        except Exception:  # noqa: BLE001 - an unreadable cooldown clock must not force a fire
            log.warning("world_news: reading the breaking cooldown clock failed", exc_info=True)
            last_push = self._now()   # fail CLOSED: assume "just fired" rather than risk a flood
        if last_push is not None and (self._now() - last_push) < cooldown_minutes * 60:
            return CommandResult(text="", ok=True, data={"fired": False, "reason": "cooldown"})

        candidates: list[tuple[str, Cluster]] = []
        for cat in _feeds():
            try:
                headlines = self._fetch_category(cat)
                clusters = self._cluster(headlines)
                qualifying = self._breaking_candidates(clusters)
                if not qualifying:
                    continue
                already = self._already_breaking_pushed(cat)
            except Exception:  # noqa: BLE001 - one category's failure must not block the rest
                log.warning("world_news: breaking check failed for %s", cat, exc_info=True)
                continue
            novel = [c for c in qualifying if all(h.url not in already for h in c.members)]
            candidates.extend((cat, c) for c in novel)

        if not candidates:
            return CommandResult(text="", ok=True, data={"fired": False})

        # Exactly one push, even if several categories qualify at once -- the cooldown covers
        # the rest; this is the "rate-limit hard" half of the rule.
        category, top = max(candidates, key=lambda pair: (pair[1].corroboration, len(pair[1].members)))
        text = self._breaking_text(category, top)
        self._notify(text)
        try:
            self._record_breaking_pushed(category, [h.url for h in top.members])
        except Exception:  # noqa: BLE001 - the push already happened; recording failure must not raise
            log.warning("world_news: recording the breaking push failed", exc_info=True)
        try:
            self._push_breaking_directive(category, top)
        except Exception:  # noqa: BLE001 - a stage push failing must never break the poll
            log.warning("world_news: pushing the breaking stage directive failed", exc_info=True)
        return CommandResult(text=text, ok=True,
                             data={"fired": True, "category": category,
                                   "corroboration": top.corroboration})

    def _push_breaking_directive(self, category: str, cluster: Cluster) -> None:
        """Phase 38's ONE catalyst path: breaking news is the sole event allowed to
        re-choreograph the stage without Calvin having said anything (the corroboration bar
        above is what keeps this rare, per AGENTOS_BUG_LIST.md #17's lesson that an
        ungated stream drowns the person it's meant to help). accent='alert' (amber) is
        reserved for exactly this case. A dead/absent stage bus is a silent no-op -- see
        core/stage.py's StageBus.push()."""
        items = self._cluster_items([cluster], max_items=_MAX_DISPLAY_ITEMS)
        widget = ArticlesWidget(topic=CATEGORY_LABEL.get(category, category), items=items)
        fact = FactWidget(
            title="CORROBORATION", stat=str(cluster.corroboration),
            sub="independent source(s) reporting this inside the last "
                f"{cluster.publish_span_hours:.1f}h — a corroboration bar, not a forecast.",
            sources=cluster.sources,
        )
        get_stage_bus().push(StageDirective(
            focus=CATEGORY_LABEL.get(category, category), transition="bloom", accent="alert",
            headline=f"Corroborated by {cluster.corroboration} independent source(s): "
                     f"{', '.join(cluster.sources)}",
            widgets=[fact, widget],
        ))

    # ------------------------------------------------------------- synthesis
    def _synthesize(self, clusters: list[Cluster]) -> str:
        if not clusters:
            return "Nothing fresh in the last day from this category's sources."
        capped = clusters[:_MAX_STORIES_TO_MODEL]
        blocks = []
        for c in capped:
            lines = "\n".join(f"  - [{h.source}] {h.title} ({h.url})" for h in c.members)
            blocks.append(f"STORY (sources: {', '.join(c.sources)}):\n{lines}")
        context = "\n\n".join(blocks)
        try:
            answer = self.llm.chat(
                "research",
                [{"role": "system", "content":
                    "You are writing one short section of a daily news briefing. Each STORY "
                    "below may list the same event reported by multiple sources — write ONE "
                    "line per STORY (never one line per source), covering what happened and "
                    "why it matters, and cite every source listed for that story by name, "
                    "e.g. '(Reuters, Al Jazeera)'. Pick the 3-6 most significant stories. Do "
                    "not invent any fact, event, or number that is not present in the "
                    "headlines below. If the stories are minor or repetitive, say that "
                    "briefly instead of padding it out."},
                 {"role": "user", "content": context}],
                max_tokens=400,
            )
        except LLMError:
            log.warning("world_news: synthesis failed, falling back to raw stories")
            answer = "\n".join(
                f"• {c.members[0].title} ({', '.join(c.sources)})" for c in capped[:6])
        return answer.strip()

    def whats_up(self, categories: str = "", notify: bool = True, full: bool = False,
                 **_: Any) -> CommandResult:
        """On-demand ("what's up today") or the scheduled morning push — identical content
        either way. `categories` (comma/space-separated: world,tech_ai,sports,business,kenya)
        narrows it; blank means everything. Include the word "full" (e.g. "/news full" or
        "/news kenya full") to force the whole 24h window instead of only what's new since
        the last ask.
        """
        feeds = _feeds()
        tokens = [c.strip().lower() for c in categories.replace(",", " ").split() if c.strip()]
        if "full" in tokens:
            full = True
            tokens = [t for t in tokens if t != "full"]
        wanted = [c for c in tokens if c in feeds] or list(feeds.keys())

        mode_note = " (full 24h window)" if full else ""
        lines = [f"🗞 WHAT'S UP TODAY{mode_note} · {time.strftime('%A %d %b', time.localtime(self._now()))}"]
        total_headlines = 0
        new_story_count = 0
        try:
            self._retire_stale_delivered()
        except Exception:  # noqa: BLE001 - housekeeping must never block the briefing
            log.warning("world_news: retiring stale delivered rows failed", exc_info=True)

        pooled_new: list[Cluster] = []  # across ALL categories, for the front page
        sections: list[tuple[str, list[Cluster], list[Cluster]]] = []  # (cat, clusters, to_show)

        for cat in wanted:
            headlines = self._fetch_category(cat)
            total_headlines += len(headlines)
            clusters = self._cluster(headlines)

            if full:
                to_show = clusters
            else:
                try:
                    delivered = self._delivered_urls(cat)
                except Exception:  # noqa: BLE001 - DB hiccup degrades to "show everything"
                    log.warning("world_news: reading delivered state failed for %s", cat, exc_info=True)
                    delivered = set()
                to_show = [c for c in clusters if any(h.url not in delivered for h in c.members)]

            new_story_count += len(to_show)
            pooled_new.extend(to_show)
            sections.append((cat, clusters, to_show))

            try:
                self._record_delivered(cat, [h.url for c in to_show for h in c.members])
            except Exception:  # noqa: BLE001 - recording must never block delivery
                log.warning("world_news: recording delivered state failed for %s", cat, exc_info=True)

        # A front page only means something when it's picking a winner ACROSS categories --
        # a single-category ask ("/news kenya") would just be repeating its own top story.
        front_page = self._front_page(pooled_new) if len(wanted) > 1 else []
        if front_page:
            lines.append("📰 TOP STORIES")
            lines.append(self._synthesize(front_page))
            lines.append("")

        for cat, clusters, to_show in sections:
            lines.append(f"\n{CATEGORY_LABEL.get(cat, cat)}")
            if not to_show and clusters:
                lines.append("Nothing new since your last check — already covered.")
            else:
                lines.append(self._synthesize(to_show))

        if total_headlines == 0:
            lines.append("\n(Couldn't reach any news sources just now — try again shortly.)")

        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, ok=total_headlines > 0,
                             data={"categories": wanted, "headline_count": total_headlines,
                                   "new_story_count": new_story_count, "full": full,
                                   "front_page": bool(front_page)})


SKILL = WorldNewsSkill()
