"""gather.py: essay-mill blocklist, full-page fetch, and the source list synthesis reads.

Regression coverage for the real bug: essay-mill sourcing (Scribd/Bartleby) produced
misinformation (a claim the Camaro "is still in production" when GM retired it in 2024).
Authoritative-only sourcing is the fix -- these tests assert the blocklist actually holds
and that only FULL page text (never a bare search snippet) reaches a source's .text.
"""

from __future__ import annotations

from skills.research.gather import (FetchedSource, gather_sources, is_blocklisted,
                                    search_authoritative)


class _FakeSearcher:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self._results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        self.queries.append(query)
        return self._results


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeFetcher:
    def __init__(self, by_url: dict[str, _FakeResponse]) -> None:
        self._by_url = by_url
        self.requested: list[str] = []

    def get(self, url: str, accept: str | None = None):
        self.requested.append(url)
        return self._by_url.get(url)


# ------------------------------------------------------------------ blocklist
def test_essay_mill_domains_are_blocklisted():
    for domain in ("scribd.com", "bartleby.com", "coursehero.com"):
        assert is_blocklisted(f"https://www.{domain}/some-essay")


def test_an_authoritative_domain_is_not_blocklisted():
    assert not is_blocklisted("https://en.wikipedia.org/wiki/Chevrolet_Camaro")
    assert not is_blocklisted("https://www.reuters.com/business/autos")


def test_a_subdomain_of_a_blocklisted_domain_is_still_blocked():
    assert is_blocklisted("https://essays.coursehero.com/some-path")


def test_extra_blocklist_domains_are_respected():
    assert is_blocklisted("https://sketchy-mill.example.com/x",
                          extra_domains=frozenset({"sketchy-mill.example.com"}))


def test_a_url_with_no_domain_is_not_blocklisted_rather_than_crashing():
    assert is_blocklisted("") is False


# ------------------------------------------------------------------ search_authoritative
def test_search_authoritative_excludes_essay_mills_from_results():
    searcher = _FakeSearcher([
        {"title": "Camaro history", "url": "https://en.wikipedia.org/wiki/Chevrolet_Camaro", "snippet": "..."},
        {"title": "Camaro essay", "url": "https://www.scribd.com/doc/12345/camaro-essay", "snippet": "..."},
        {"title": "Camaro book report", "url": "https://www.bartleby.com/essay/camaro", "snippet": "..."},
    ])

    kept = search_authoritative(searcher, "Camaro", max_results=6)

    urls = [r["url"] for r in kept]
    assert "https://en.wikipedia.org/wiki/Chevrolet_Camaro" in urls
    assert not any("scribd.com" in u or "bartleby.com" in u for u in urls)


def test_search_authoritative_overfetches_so_blocklisted_hits_dont_starve_the_result_set():
    many_mill_hits = [{"title": f"essay {i}", "url": f"https://www.scribd.com/doc/{i}", "snippet": ""}
                      for i in range(10)]
    good_hit = {"title": "Real source", "url": "https://www.reuters.com/x", "snippet": "..."}
    searcher = _FakeSearcher(many_mill_hits + [good_hit])

    kept = search_authoritative(searcher, "Camaro", max_results=3)

    assert any(r["url"] == good_hit["url"] for r in kept)


# ------------------------------------------------------------------ full-page fetch
def test_gather_sources_uses_full_page_text_not_the_snippet():
    url = "https://en.wikipedia.org/wiki/Chevrolet_Camaro"
    searcher = _FakeSearcher([{"title": "Camaro", "url": url, "snippet": "short snippet only"}])
    html = "<html><body><script>ignored</script><p>The sixth generation ended in 2024.</p></body></html>"
    fetcher = _FakeFetcher({url: _FakeResponse(200, html)})

    sources = gather_sources(searcher, fetcher, "Camaro")

    assert len(sources) == 1
    assert "2024" in sources[0].text
    assert "short snippet only" != sources[0].text
    assert "ignored" not in sources[0].text


def test_gather_sources_degrades_to_the_snippet_when_the_fetch_fails():
    url = "https://en.wikipedia.org/wiki/Chevrolet_Camaro"
    searcher = _FakeSearcher([{"title": "Camaro", "url": url, "snippet": "fallback snippet"}])
    fetcher = _FakeFetcher({})  # no entry -> .get() returns None

    sources = gather_sources(searcher, fetcher, "Camaro")

    assert len(sources) == 1
    assert sources[0].text == "fallback snippet"


def test_gather_sources_drops_a_result_with_neither_full_text_nor_a_snippet():
    url = "https://en.wikipedia.org/wiki/Chevrolet_Camaro"
    searcher = _FakeSearcher([{"title": "Camaro", "url": url, "snippet": ""}])
    fetcher = _FakeFetcher({})

    assert gather_sources(searcher, fetcher, "Camaro") == []


def test_gather_sources_never_includes_a_blocklisted_domain_even_if_fetchable():
    url = "https://www.scribd.com/doc/12345/camaro-essay"
    searcher = _FakeSearcher([{"title": "Camaro essay", "url": url, "snippet": "..."}])
    fetcher = _FakeFetcher({url: _FakeResponse(200, "<p>whatever</p>")})

    assert gather_sources(searcher, fetcher, "Camaro") == []


def test_gather_sources_numbers_sources_sequentially_from_one():
    urls = ["https://a.test/1", "https://b.test/2"]
    searcher = _FakeSearcher([{"title": "A", "url": urls[0], "snippet": "a"},
                              {"title": "B", "url": urls[1], "snippet": "b"}])
    fetcher = _FakeFetcher({})

    sources = gather_sources(searcher, fetcher, "q")

    assert [s.n for s in sources] == [1, 2]


def test_a_fetch_that_raises_degrades_to_the_snippet_not_a_crash():
    url = "https://en.wikipedia.org/wiki/Chevrolet_Camaro"
    searcher = _FakeSearcher([{"title": "Camaro", "url": url, "snippet": "fallback"}])

    class _BoomFetcher:
        def get(self, *a, **k):
            raise RuntimeError("network exploded")

    sources = gather_sources(searcher, _BoomFetcher(), "Camaro")
    assert sources[0].text == "fallback"
