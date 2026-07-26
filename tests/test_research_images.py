"""images.py: Wikimedia Commons figures, license-verified before ever being embedded.

Hard content-safety line: an image is used ONLY when Commons's own metadata verifies an
open license. No open license anywhere in the candidate list -> None (the caller renders
the typed placeholder) -- never an unverified image, never a search substitute.
"""

from __future__ import annotations

import json

from skills.research.images import WikimediaImages

# A real, tiny, valid 1x1 transparent PNG -- ReportLab's Image flowable needs bytes it can
# actually decode, not an arbitrary placeholder string.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
    "0000001249444154789c631437d5666060606200030005aa007ba595dbb9000000"
    "0049454e44ae426082")


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, content: bytes | None = None) -> None:
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeFetcher:
    def __init__(self, by_url: dict[str, _FakeResponse]) -> None:
        self._by_url = by_url
        self.requested: list[str] = []

    def get(self, url: str, accept: str | None = None):
        self.requested.append(url)
        for key, resp in self._by_url.items():
            if key in url:
                return resp
        return None


def _search_response(titles: list[str]) -> _FakeResponse:
    return _FakeResponse(200, {"query": {"search": [{"title": t} for t in titles]}})


def _imageinfo_response(url: str, license_name: str, license_url: str = "",
                        artist: str = "Jane Doe") -> _FakeResponse:
    return _FakeResponse(200, {"query": {"pages": {"1": {"imageinfo": [{
        "url": url,
        "extmetadata": {
            "LicenseShortName": {"value": license_name},
            "LicenseUrl": {"value": license_url},
            "Artist": {"value": artist},
        },
    }]}}}})


def test_finds_and_downloads_an_open_licensed_image(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Camaro.jpg"]),
        "titles=File%3ACamaro.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Camaro.jpg", "CC BY-SA 4.0"),
        "upload.wikimedia.org/Camaro.jpg": _FakeResponse(200, content=_PNG_BYTES),
    })
    images = WikimediaImages(fetcher=fetcher)

    result = images.find_image("Chevrolet Camaro", tmp_path)

    assert result is not None
    assert result.path.exists()
    assert result.path.read_bytes() == _PNG_BYTES
    assert "Jane Doe" in result.attribution
    assert "CC BY-SA 4.0" in result.attribution


def test_public_domain_is_recognized_as_open(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Old.jpg"]),
        "titles=File%3AOld.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Old.jpg", "Public domain"),
        "upload.wikimedia.org/Old.jpg": _FakeResponse(200, content=_PNG_BYTES),
    })
    images = WikimediaImages(fetcher=fetcher)

    result = images.find_image("Old thing", tmp_path)

    assert result is not None
    assert "Public domain" in result.attribution


def test_a_non_open_license_is_rejected_and_no_image_is_used(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Restricted.jpg"]),
        "titles=File%3ARestricted.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Restricted.jpg", "All Rights Reserved"),
    })
    images = WikimediaImages(fetcher=fetcher)

    result = images.find_image("Something", tmp_path)

    assert result is None


def test_falls_through_to_the_next_candidate_when_the_first_is_not_open(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Restricted.jpg", "File:Open.jpg"]),
        "titles=File%3ARestricted.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Restricted.jpg", "All Rights Reserved"),
        "titles=File%3AOpen.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Open.jpg", "CC0 1.0"),
        "upload.wikimedia.org/Open.jpg": _FakeResponse(200, content=_PNG_BYTES),
    })
    images = WikimediaImages(fetcher=fetcher)

    result = images.find_image("Something", tmp_path)

    assert result is not None
    assert "CC0" in result.attribution


def test_no_search_results_returns_none(tmp_path):
    fetcher = _FakeFetcher({"list=search": _search_response([])})
    images = WikimediaImages(fetcher=fetcher)
    assert images.find_image("Nothing findable", tmp_path) is None


def test_search_endpoint_unreachable_returns_none_not_a_crash(tmp_path):
    fetcher = _FakeFetcher({})  # every .get() returns None
    images = WikimediaImages(fetcher=fetcher)
    assert images.find_image("Anything", tmp_path) is None


def test_image_download_failure_falls_through_to_no_image(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Open.jpg"]),
        "titles=File%3AOpen.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Open.jpg", "CC BY 2.0"),
        # download URL deliberately absent -> .get() returns None
    })
    images = WikimediaImages(fetcher=fetcher)
    assert images.find_image("Something", tmp_path) is None


def test_a_raising_fetcher_degrades_to_no_image_not_a_crash(tmp_path):
    class _BoomFetcher:
        def get(self, *a, **k):
            raise RuntimeError("network exploded")

    images = WikimediaImages(fetcher=_BoomFetcher())
    assert images.find_image("Anything", tmp_path) is None


def test_attribution_strips_html_tags_from_the_artist_field(tmp_path):
    fetcher = _FakeFetcher({
        "list=search": _search_response(["File:Camaro.jpg"]),
        "titles=File%3ACamaro.jpg": _imageinfo_response(
            "https://upload.wikimedia.org/Camaro.jpg", "CC BY-SA 4.0",
            artist='<a href="//example.com/user/Jane">Jane Doe</a>'),
        "upload.wikimedia.org/Camaro.jpg": _FakeResponse(200, content=_PNG_BYTES),
    })
    images = WikimediaImages(fetcher=fetcher)

    result = images.find_image("Chevrolet Camaro", tmp_path)

    assert result is not None
    assert "<a" not in result.attribution
    assert "Jane Doe" in result.attribution
