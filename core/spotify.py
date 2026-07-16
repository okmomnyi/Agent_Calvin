"""Spotify Web API client (Phase 22).

READ THIS BEFORE ADDING ENDPOINTS. Spotify restricted the Web API in stages (Nov 2024, then
Feb 2026). For an app created now:

  * the connected account must have **Premium**, or the integration stops working (playback
    control needs Premium anyway);
  * **Recommendations, Audio Features, Audio Analysis, Related Artists and Featured
    Playlists are GONE** for new apps — so there is no official BPM/key/energy data and no
    official "recommend me tracks" call to build on. This client therefore does not define
    those methods at all: reaching for them would just 403;
  * playlist creation is scoped to Calvin's own account (`POST /users/{his_id}/playlists`,
    with the id read back from `/me`) — fine here;
  * the Web API **cannot mix audio** at any tier. It issues playback commands. Real
    crossfading/beatmatching is not available to any third-party app.

What remains — and is all we use: recently-played, top tracks/artists, saved library,
search, queue, playlist create/add, and playback transport. The taste model in skills/music
is built from those; sequencing reasoning comes from the LLM's own knowledge, never from a
deprecated Spotify endpoint.

The refresh token lives in the environment (never plaintext in the DB); access tokens are
held in memory only.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any

import requests

from core.logging_setup import get_logger

log = get_logger("core.spotify")

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTH_URL = "https://accounts.spotify.com/authorize"

# Everything we need, and nothing we don't.
SCOPES = [
    "user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
    "user-read-recently-played", "user-top-read", "user-library-read",
    "playlist-modify-private", "playlist-modify-public",
]

# Endpoints Spotify removed for new apps — named here so nobody re-adds them by accident.
DEPRECATED_FOR_NEW_APPS = ("/recommendations", "/audio-features", "/audio-analysis",
                           "/artists/{id}/related-artists", "/browse/featured-playlists")

# `{id}` is a placeholder for a real id, so these can only be matched as patterns — compared
# literally, `/artists/{id}/related-artists` never matches the `/artists/4Yj.../...` we'd send.
_DEPRECATED_RE = re.compile("|".join(
    re.escape(p).replace(r"\{id\}", "[^/]+") + r"(?:\?|/|$)"
    for p in DEPRECATED_FOR_NEW_APPS))


class SpotifyError(RuntimeError):
    """Raised when Spotify cannot be reached, or auth/Premium is missing."""


class SpotifyClient:
    """Thin client over the endpoints that still exist. Inject `session` for tests."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------- auth
    @staticmethod
    def authorize_url(redirect_uri: str) -> str:
        """One-time consent URL for `manage.py music connect`."""
        cid = os.getenv("SPOTIFY_CLIENT_ID", "")
        if not cid:
            # Without this we'd hand over a URL with `client_id=` empty, and only fail after
            # the user had gone and approved it.
            raise SpotifyError("SPOTIFY_CLIENT_ID is not set — add it to .env first.")
        from urllib.parse import urlencode

        return AUTH_URL + "?" + urlencode({
            "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES)})

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Swap the one-time code for a refresh token (printed once, stored in .env)."""
        data = self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": redirect_uri})
        token = data.get("refresh_token", "")
        if not token:
            raise SpotifyError("Spotify did not return a refresh token.")
        return token

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        cid, secret = os.getenv("SPOTIFY_CLIENT_ID", ""), os.getenv("SPOTIFY_CLIENT_SECRET", "")
        if not cid or not secret:
            raise SpotifyError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET are not set.")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = self.session.post(TOKEN_URL, data=payload,
                                 headers={"Authorization": f"Basic {basic}"}, timeout=20)
        if resp.status_code != 200:
            raise SpotifyError(f"Spotify token error {resp.status_code}: {resp.text[:160]}")
        return resp.json()

    def _token(self) -> str:
        """Access token from the refresh token, cached in memory until it expires."""
        if self._access_token and time.time() < self._expires_at - 30:
            return self._access_token
        refresh = os.getenv("SPOTIFY_REFRESH_TOKEN", "")
        if not refresh:
            raise SpotifyError("SPOTIFY_REFRESH_TOKEN is not set — run `manage.py music connect`.")
        data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    # ------------------------------------------------------------- transport
    def _call(self, method: str, path: str, **kw: Any) -> Any:
        if _DEPRECATED_RE.match(path):                       # belt and braces
            raise SpotifyError(f"{path} is deprecated for new apps — do not call it.")
        resp = self.session.request(method, API + path,
                                    headers={"Authorization": f"Bearer {self._token()}"},
                                    timeout=20, **kw)
        if resp.status_code == 403:
            raise SpotifyError("Spotify returned 403 — the account likely isn't Premium, or the "
                               "app lacks that scope.")
        if resp.status_code == 404:
            raise SpotifyError("No active Spotify device — open Spotify somewhere first.")
        if resp.status_code >= 400:
            raise SpotifyError(f"Spotify {resp.status_code}: {resp.text[:160]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------- taste inputs (still available)
    def me(self) -> dict[str, Any]:
        return self._call("GET", "/me")

    def is_premium(self) -> bool:
        try:
            return self.me().get("product") == "premium"
        except SpotifyError:
            return False

    def recently_played(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._call("GET", f"/me/player/recently-played?limit={limit}")
        return [i["track"] for i in data.get("items", []) if i.get("track")]

    def top_tracks(self, time_range: str = "medium_term", limit: int = 30) -> list[dict[str, Any]]:
        return self._call("GET", f"/me/top/tracks?time_range={time_range}&limit={limit}").get("items", [])

    def top_artists(self, time_range: str = "medium_term", limit: int = 30) -> list[dict[str, Any]]:
        return self._call("GET", f"/me/top/artists?time_range={time_range}&limit={limit}").get("items", [])

    def saved_tracks(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._call("GET", f"/me/tracks?limit={limit}")
        return [i["track"] for i in data.get("items", []) if i.get("track")]

    # ------------------------------------------------------------- search (how we resolve ideas)
    def search_track(self, query: str) -> dict[str, Any] | None:
        """Resolve a track idea to a REAL Spotify track — nothing is suggested unverified."""
        from urllib.parse import quote_plus

        items = self._call("GET", f"/search?q={quote_plus(query)}&type=track&limit=1") \
            .get("tracks", {}).get("items", [])
        return items[0] if items else None

    def search_artist(self, query: str) -> dict[str, Any] | None:
        from urllib.parse import quote_plus

        items = self._call("GET", f"/search?q={quote_plus(query)}&type=artist&limit=1") \
            .get("artists", {}).get("items", [])
        return items[0] if items else None

    # ------------------------------------------------------------- playback (Premium)
    def devices(self) -> list[dict[str, Any]]:
        return self._call("GET", "/me/player/devices").get("devices", [])

    def queue(self, uri: str) -> None:
        from urllib.parse import quote

        self._call("POST", f"/me/player/queue?uri={quote(uri)}")

    def play(self) -> None:
        self._call("PUT", "/me/player/play")

    def pause(self) -> None:
        self._call("PUT", "/me/player/pause")

    def next_track(self) -> None:
        self._call("POST", "/me/player/next")

    def previous_track(self) -> None:
        self._call("POST", "/me/player/previous")

    def set_volume(self, percent: int) -> None:
        self._call("PUT", f"/me/player/volume?volume_percent={max(0, min(100, int(percent)))}")

    def transfer(self, device_id: str) -> None:
        self._call("PUT", "/me/player", json={"device_ids": [device_id], "play": True})

    def now_playing(self) -> dict[str, Any]:
        return self._call("GET", "/me/player/currently-playing") or {}

    # ------------------------------------------------------------- playlists (own account only)
    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict[str, Any]:
        user_id = self.me()["id"]
        return self._call("POST", f"/users/{user_id}/playlists",
                          json={"name": name, "description": description[:300], "public": public})

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        self._call("POST", f"/playlists/{playlist_id}/tracks", json={"uris": uris[:100]})
