"""Openly-licensed figures for a research report, via Wikimedia Commons's free API.

Hard content-safety line (Phase 39): an image is embedded ONLY after its license is
verified open (CC / public domain) straight off Commons's own metadata -- never an image
search, never an unlicensed/unverified picture. No suitable open image -> the caller gets
None and renders the typed placeholder box instead; there is no third path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from core.logging_setup import get_logger

log = get_logger("skills.research.images")

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Substrings of Commons's own LicenseShortName/LicenseUrl metadata that mean "open" --
# checked against the license text Commons itself reports, never guessed from a filename
# or a page title.
_OPEN_LICENSE_MARKERS = ("cc0", "cc-by", "cc by", "public domain", "pd-old", "pdm")


@dataclass
class ImageResult:
    path: Path
    caption: str
    attribution: str
    license_name: str


def _is_open_license(license_short_name: str, license_url: str) -> bool:
    text = f"{license_short_name} {license_url}".lower()
    return any(marker in text for marker in _OPEN_LICENSE_MARKERS)


def _strip_html(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _safe_filename(title: str) -> str:
    import re

    name = title.split(":", 1)[-1]  # "File:Foo.jpg" -> "Foo.jpg"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "image"


class WikimediaImages:
    """Injectable fetcher (the same shared rate-limited Fetcher every other skill uses)
    so tests never touch the real Commons API or the network."""

    def __init__(self, fetcher: Any | None = None) -> None:
        self._fetcher = fetcher

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            from skills.job_hunter.fetcher import Fetcher

            # Commons's own documented public API -- meant to be polled by tools, not an
            # article/content page (same reasoning markets.py/world_news.py already give
            # for their own respect_robots=False price/feed endpoints).
            self._fetcher = Fetcher(respect_robots=False)
        return self._fetcher

    def find_image(self, topic: str, download_dir: Path) -> ImageResult | None:
        """The first Commons search hit whose license verifies as open. Returns None (the
        caller's cue to render the typed placeholder) on no results, no verifiably-open
        hit, or any transport/parse failure -- an image is never worth a report failing."""
        try:
            candidates = self._search(topic)
        except Exception:  # noqa: BLE001 - a broken search must degrade to "no image"
            log.warning("research: Wikimedia search failed for %r", topic, exc_info=True)
            return None

        for title in candidates:
            try:
                result = self._try_candidate(title, download_dir)
            except Exception:  # noqa: BLE001 - one bad candidate must not block the rest
                log.warning("research: Wikimedia candidate %r failed", title, exc_info=True)
                continue
            if result is not None:
                return result
        return None

    def _try_candidate(self, title: str, download_dir: Path) -> ImageResult | None:
        info = self._image_info(title)
        if info is None:
            return None
        url, license_name, license_url, artist = info
        if not _is_open_license(license_name, license_url):
            return None
        resp = self.fetcher.get(url, accept="image/*")
        if resp is None or resp.status_code != 200:
            return None
        content = getattr(resp, "content", None)
        if not content:
            return None
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / _safe_filename(title)
        path.write_bytes(content)
        author = _strip_html(artist) or "Unknown author"
        attribution = f"{author} — Wikimedia Commons ({license_name or 'open license'})"
        return ImageResult(path=path, caption=attribution, attribution=attribution,
                           license_name=license_name)

    def _search(self, topic: str) -> list[str]:
        url = (f"{_COMMONS_API}?action=query&list=search&srnamespace=6&format=json"
              f"&srlimit=5&srsearch={quote_plus(topic)}")
        resp = self.fetcher.get(url, accept="application/json")
        if resp is None or resp.status_code != 200:
            return []
        try:
            hits = resp.json()["query"]["search"]
        except (ValueError, KeyError, TypeError):
            return []
        return [h["title"] for h in hits if h.get("title")]

    def _image_info(self, title: str) -> tuple[str, str, str, str] | None:
        url = (f"{_COMMONS_API}?action=query&prop=imageinfo&iiprop=url%7Cextmetadata"
              f"&format=json&titles={quote_plus(title)}")
        resp = self.fetcher.get(url, accept="application/json")
        if resp is None or resp.status_code != 200:
            return None
        try:
            pages = resp.json()["query"]["pages"]
            page = next(iter(pages.values()))
            info = page["imageinfo"][0]
            meta = info.get("extmetadata") or {}
            license_name = (meta.get("LicenseShortName") or {}).get("value", "")
            license_url = (meta.get("LicenseUrl") or {}).get("value", "")
            artist = (meta.get("Artist") or {}).get("value", "")
            return info["url"], license_name, license_url, artist
        except (ValueError, KeyError, TypeError, IndexError, StopIteration):
            return None
