"""Job source tests: JSON/RSS/HTML parsers against fixtures + polite Fetcher behavior."""

from __future__ import annotations

import json

from skills.job_hunter.fetcher import Fetcher
from skills.job_hunter.sources.base import keyword_category, stable_id
from skills.job_hunter.sources.jsonapi import parse_jobicy, parse_remoteok, parse_remotive
from skills.job_hunter.sources.rss import parse_feed
from skills.job_hunter.sources.serpapi import parse_serpapi
from skills.job_hunter.sources.watched import extract_job_links


# ------------------------------------------------------------------ fixtures
REMOTEOK = json.dumps([
    {"legal": "By using this data you agree..."},
    {"id": "111", "position": "DevOps Engineer", "company": "Acme",
     "tags": ["devops", "aws"], "description": "Manage k8s clusters", "url": "https://r.ok/111",
     "location": "Remote"},
    {"id": "222", "position": "Audio Transcriber", "company": "VoiceCo",
     "tags": ["transcription"], "description": "Transcribe audio files", "url": "https://r.ok/222"},
])

REMOTIVE = json.dumps({"jobs": [
    {"id": 9, "title": "Cloud Support Engineer", "company_name": "NimbusCo",
     "url": "https://remotive/9", "tags": ["cloud", "linux"],
     "candidate_required_location": "Worldwide", "description": "Support AWS customers"},
]})

JOBICY = json.dumps({"jobs": [
    {"id": 5, "jobTitle": "Junior SRE", "companyName": "Reliable Inc",
     "url": "https://jobicy/5", "jobIndustry": ["DevOps"], "jobGeo": "Anywhere",
     "jobExcerpt": "On-call, monitoring, terraform"},
]})

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>CloudCorp: Kubernetes Engineer</title><link>https://wwr/1</link>
<guid>wwr-1</guid><description>&lt;p&gt;Run &lt;b&gt;k8s&lt;/b&gt; in prod&lt;/p&gt;</description></item>
<item><title>Plain Transcription Job</title><link>https://wwr/2</link><guid>wwr-2</guid>
<description>Transcribe interviews</description></item>
</channel></rss>"""

SERPAPI = json.dumps({"jobs_results": [
    {"job_id": "g1", "title": "DevOps Intern", "company_name": "StartTech",
     "description": "Learn CI/CD", "location": "Nairobi",
     "apply_options": [{"link": "https://apply/g1"}]},
]})

CAREERS_HTML = """<html><body>
<a href="/careers/devops-engineer">DevOps Engineer</a>
<a href="/about">About us</a>
<a href="https://ext.com/jobs/123">Cloud Architect</a>
<a href="#top">Top</a>
</body></html>"""


# ------------------------------------------------------------------ JSON parsers
def test_parse_remoteok_skips_legal_and_categorizes():
    jobs = parse_remoteok(json.loads(REMOTEOK))
    assert len(jobs) == 2
    assert jobs[0].title == "DevOps Engineer"
    assert jobs[0].category_hint == "cloud_devops"
    assert jobs[1].category_hint == "transcription"
    assert jobs[0].source == "remoteok"


def test_parse_remotive():
    jobs = parse_remotive(json.loads(REMOTIVE))
    assert len(jobs) == 1
    assert jobs[0].company == "NimbusCo"
    assert jobs[0].category_hint == "cloud_devops"


def test_parse_jobicy():
    jobs = parse_jobicy(json.loads(JOBICY))
    assert jobs[0].title == "Junior SRE"
    assert "DevOps" in jobs[0].tags


def test_parse_serpapi_uses_apply_link():
    jobs = parse_serpapi(json.loads(SERPAPI))
    assert jobs[0].url == "https://apply/g1"
    assert jobs[0].category_hint == "internship"  # 'intern' keyword wins


# ------------------------------------------------------------------ RSS parser
def test_parse_feed_splits_company_and_strips_html():
    jobs = parse_feed(RSS, "weworkremotely")
    assert len(jobs) == 2
    assert jobs[0].company == "CloudCorp"
    assert jobs[0].title == "Kubernetes Engineer"
    assert "<b>" not in jobs[0].description and "k8s" in jobs[0].description
    assert jobs[0].external_id == "wwr-1"


def test_parse_feed_category_hint_override():
    jobs = parse_feed(RSS, "wwr_devops", category_hint="cloud_devops")
    assert all(j.category_hint == "cloud_devops" for j in jobs)


# Regression (#19): "[3589] DevOps Engineer @ " with a blank company -- himalayas.app-style
# feeds don't use WWR's "Company: Title" format and have no <author> element either; the
# company sits in <dc:creator> instead, which the parser never checked.
RSS_DC_CREATOR = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>DevOps Engineer</title><link>https://himalayas/1</link><guid>him-1</guid>
<dc:creator>Nimbus Cloud</dc:creator><description>Run infra</description></item>
</channel></rss>"""


def test_parse_feed_falls_back_to_dc_creator_for_company():
    jobs = parse_feed(RSS_DC_CREATOR, "himalayas")
    assert jobs[0].title == "DevOps Engineer"
    assert jobs[0].company == "Nimbus Cloud"


def test_parse_feed_with_no_company_signal_anywhere_stays_honestly_empty():
    """No colon in the title, no author, no dc:creator -- company must stay '', never a
    guess. The digest layer (skill.py's _render_digest) is what turns '' into an honest
    "company not listed" line; the parser itself must not invent one."""
    jobs = parse_feed(RSS, "weworkremotely")
    assert jobs[1].title == "Plain Transcription Job"
    assert jobs[1].company == ""


# ------------------------------------------------------------------ watched HTML
def test_extract_job_links_filters_non_job_links():
    jobs = extract_job_links(CAREERS_HTML, "https://co.com/careers", "CoCo")
    urls = {j.url for j in jobs}
    assert "https://co.com/careers/devops-engineer" in urls
    assert "https://ext.com/jobs/123" in urls
    assert "https://co.com/about" not in urls  # not job-like
    assert all(j.company == "CoCo" for j in jobs)


# ------------------------------------------------------------------ helpers
def test_keyword_category():
    assert keyword_category("Senior DevOps Engineer") == "cloud_devops"
    assert keyword_category("Subtitling & captioning specialist") == "transcription"
    assert keyword_category("Software Engineering Intern") == "internship"
    assert keyword_category("Marketing Manager") == "other"


def test_stable_id_deterministic():
    assert stable_id("a", "b") == stable_id("a", "b")
    assert stable_id("a", "b") != stable_id("a", "c")


# ------------------------------------------------------------------ Fetcher
class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.headers = {}


class _Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        self._last_headers = headers
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return _Resp(status=404)


def test_fetcher_respects_robots_disallow():
    sess = _Session({
        "robots.txt": _Resp(text="User-agent: *\nDisallow: /", status=200),
        "/jobs": _Resp(text="secret", status=200),
    })
    f = Fetcher(session=sess, min_interval=0, sleep=lambda s: None)
    assert f.get("https://site.com/jobs") is None
    assert not any(u.endswith("/jobs") for u in sess.calls)  # never fetched the disallowed path


def test_fetcher_sends_useragent_when_allowed():
    sess = _Session({
        "robots.txt": _Resp(text="User-agent: *\nDisallow: /private", status=200),
        "/jobs": _Resp(text="ok", status=200),
    })
    f = Fetcher(session=sess, min_interval=0, sleep=lambda s: None)
    resp = f.get("https://site.com/jobs")
    assert resp is not None and resp.text == "ok"
    assert "AgentOS" in sess._last_headers["User-Agent"]
