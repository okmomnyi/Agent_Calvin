"""Open URLs (Phase 36 Slice 3).

The property that matters most: only http/https ever reaches a `client_actions` payload.
`file:`, `javascript:`, and anything else are refused outright, both at the skill's own
`validate_url` and again at kernel/app.py's `_client_actions` narrow waist.
"""

from __future__ import annotations

import pytest

from skills.web_open import WebOpenSkill, validate_url


@pytest.fixture
def web_open(mem):
    return WebOpenSkill(memory=mem)


# ================================================================= scheme allowlist
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "ftp://example.com/file",
    "not a url",
    "",
])
def test_disallowed_or_malformed_urls_are_rejected(url):
    assert validate_url(url) is not None


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/path?q=1"])
def test_http_and_https_are_allowed(url):
    assert validate_url(url) is None


def test_open_refuses_a_file_url_even_as_a_raw_url(web_open):
    result = web_open.open(url="file:///etc/passwd")
    assert result.ok is False
    assert "client_actions" not in result.data


def test_open_refuses_a_javascript_url(web_open):
    result = web_open.open(url="javascript:alert(document.cookie)")
    assert result.ok is False


# ================================================================= favourites CRUD
def test_add_then_open_by_name(web_open):
    web_open.add(name="dashboard", url="https://example.com/dashboard")
    result = web_open.open(name="dashboard")
    assert result.ok is True
    assert result.data["client_actions"] == [
        {"op": "open_url", "url": "https://example.com/dashboard"}]


def test_add_rejects_a_bad_url_before_it_reaches_the_table(web_open):
    result = web_open.add(name="evil", url="javascript:alert(1)")
    assert result.ok is False
    assert web_open.mem.execute(
        "SELECT * FROM url_favourites WHERE name='evil'").fetchone() is None


def test_open_unknown_favourite_lists_what_is_known(web_open):
    web_open.add(name="dashboard", url="https://example.com")
    result = web_open.open(name="nope")
    assert result.ok is False
    assert "dashboard" in result.text


def test_open_accepts_a_raw_url_without_a_saved_favourite(web_open):
    result = web_open.open(url="https://example.com/anything")
    assert result.ok is True
    assert result.data["client_actions"][0]["url"] == "https://example.com/anything"


# ================================================================= retire (never delete)
def test_retire_does_not_delete_the_row(web_open):
    web_open.add(name="dashboard", url="https://example.com")
    web_open.retire(name="dashboard")
    row = web_open.mem.execute(
        "SELECT * FROM url_favourites WHERE name='dashboard'").fetchone()
    assert row is not None
    assert row["retired_at"] is not None


def test_retired_favourite_is_not_openable_by_name(web_open):
    web_open.add(name="dashboard", url="https://example.com")
    web_open.retire(name="dashboard")
    result = web_open.open(name="dashboard")
    assert result.ok is False


def test_re_adding_a_retired_favourite_reactivates_it(web_open):
    web_open.add(name="dashboard", url="https://example.com")
    web_open.retire(name="dashboard")
    web_open.add(name="dashboard", url="https://example.com/v2")
    result = web_open.open(name="dashboard")
    assert result.ok is True
    assert result.data["client_actions"][0]["url"] == "https://example.com/v2"


def test_retiring_an_unknown_favourite_is_reported_not_silently_ok(web_open):
    result = web_open.retire(name="never-existed")
    assert result.ok is False
