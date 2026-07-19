"""Full-posting enrichment (Phase 34b).

The sources hand back stubs -- across Calvin's 42 pending jobs the median description was 162
characters and not one mentioned a deadline. These tests are mostly about restraint: the fetch
is best-effort, and a failure must never cost him the posting.
"""

from __future__ import annotations

import json

import pytest

from skills.job_hunter.enrich import MAX_CHARS, enrich_job, fetch_description, html_to_text


class FakeResponse:
    def __init__(self, text: str = "") -> None:
        self.text = text


class FakeFetcher:
    """Stands in for the polite Fetcher; records what was requested."""

    def __init__(self, body: str | None = "", explode: bool = False) -> None:
        self.body = body
        self.explode = explode
        self.urls: list[str] = []

    def get(self, url: str, **_):
        self.urls.append(url)
        if self.explode:
            raise RuntimeError("blocked by robots")
        return None if self.body is None else FakeResponse(self.body)


POSTING = """
<html><head><title>x</title><style>.a{color:red}</style></head>
<body>
  <nav>Home Jobs Login</nav>
  <h1>Cloud Engineer Intern</h1>
  <p>You will run Kubernetes clusters and CI/CD pipelines on AWS.</p>
  <p>Application deadline: 25 December 2026.</p>
  <script>track()</script>
  <footer>Cookie policy</footer>
</body></html>
"""


# --------------------------------------------------------------------- extraction
def test_extracts_the_posting_and_drops_the_furniture():
    text = html_to_text(POSTING)
    assert "Kubernetes clusters" in text
    for noise in ("track()", "color:red", "Cookie policy", "Home Jobs Login"):
        assert noise not in text, f"kept page furniture: {noise!r}"


@pytest.mark.parametrize("junk", ["", "   ", "<<<not really html", "\x00\x01"])
def test_junk_input_returns_empty_rather_than_raising(junk):
    assert isinstance(html_to_text(junk), str)


def test_caps_a_runaway_page():
    assert len(html_to_text("<p>" + "x " * 200_000 + "</p>")) <= MAX_CHARS


# --------------------------------------------------------------------- fetching
def test_fetches_through_the_shared_fetcher():
    """Politeness is inherited -- robots.txt, the per-host floor and the UA all live there."""
    fetcher = FakeFetcher(POSTING)
    assert "Kubernetes" in fetch_description("https://x.test/job/1", fetcher)
    assert fetcher.urls == ["https://x.test/job/1"]


@pytest.mark.parametrize("fetcher", [FakeFetcher(explode=True), FakeFetcher(None),
                                     FakeFetcher("")])
def test_a_failed_fetch_is_not_an_error(fetcher):
    """A dead link, a JS-only page, a robots block. Enrichment must never abort a hunt."""
    assert fetch_description("https://x.test/job/1", fetcher) == ""


def test_no_url_means_no_request():
    fetcher = FakeFetcher(POSTING)
    assert fetch_description("", fetcher) == ""
    assert fetcher.urls == []


# --------------------------------------------------------------------- folding it in
def _stub_job(mem, description: str = "Cloud Engineer Intern. Apply now.") -> int:
    mem.upsert_job("test", "e1", title="Cloud Engineer Intern", company="Acme",
                   url="https://x.test/job/1",
                   raw_json=json.dumps({"description": description}))
    return mem.get_job_by_ref("test", "e1")["id"]


def test_replaces_the_stub_and_finds_the_deadline(mem):
    job_id = _stub_job(mem)
    text = enrich_job(job_id, "https://x.test/job/1", memory=mem, fetcher=FakeFetcher(POSTING))

    assert "Kubernetes" in text
    row = mem.get_job(job_id)
    raw = json.loads(row["raw_json"])
    assert "Kubernetes" in raw["description"]
    assert raw["enriched"] is True
    assert row["deadline"] is not None, "the whole point: a deadline expiry can act on"


def test_keeps_the_stub_alongside(mem):
    """The fetched page is better text, but it comes from an unknown template. Losing the
    source's own clean summary to a bad extraction would be a silent downgrade."""
    job_id = _stub_job(mem, description="Clean source summary.")
    enrich_job(job_id, "https://x.test/job/1", memory=mem, fetcher=FakeFetcher(POSTING))
    raw = json.loads(mem.get_job(job_id)["raw_json"])
    assert raw["description_stub"] == "Clean source summary."


def test_only_upgrades(mem):
    """Some pages extract to LESS than the source already gave us."""
    long_stub = "Detailed source description. " * 50
    job_id = _stub_job(mem, description=long_stub)
    assert enrich_job(job_id, "https://x.test/job/1", memory=mem,
                      fetcher=FakeFetcher("<p>Sign in to view</p>")) == ""
    raw = json.loads(mem.get_job(job_id)["raw_json"])
    assert raw["description"] == long_stub, "traded good text for a login wall"


def test_a_failed_fetch_leaves_the_job_untouched(mem):
    job_id = _stub_job(mem, description="Original stub.")
    assert enrich_job(job_id, "https://x.test/job/1", memory=mem,
                      fetcher=FakeFetcher(explode=True)) == ""
    raw = json.loads(mem.get_job(job_id)["raw_json"])
    assert raw["description"] == "Original stub."
    assert "enriched" not in raw
