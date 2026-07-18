"""Music companion (Phase 22). All Spotify calls are mocked.

What must hold, given Spotify's real restrictions:
  * we never call an endpoint Spotify removed for new apps (Recommendations / Audio-Features
    / Related-Artists / Featured-Playlists) — the client refuses even if asked;
  * nothing is queued or suggested that wasn't RESOLVED to a real track/artist via Search;
  * discovery is framed as OUR suggestion, never as Spotify's recommendation;
  * "DJ mode" is sequencing + narration and says so — the Web API cannot mix audio;
  * transition lines use a stock voice (§0 P9) — there is no cloning path;
  * Calvin's standing music rules are honoured (and 'music' is a category this skill declares).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.llm import LLMClient
from core.persona_store import PersonaEngine
from core.skill import UNIVERSAL_INVARIANTS, CommandResult
from core.spotify import (DEPRECATED_FOR_NEW_APPS, _DEPRECATED_RE, SpotifyClient,
                          SpotifyError)
from skills.music import MusicSkill

def at_hour(hour: int) -> float:
    """An epoch that is `hour` o'clock in CALVIN's timezone — not the test machine's.

    Rules like "before 8am" are evaluated in his tz, so the tests must build their clock the
    same way or they'd pass/fail depending on where they run.
    """
    from datetime import datetime

    from core.config import get_settings

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(get_settings().tz)
        return datetime(2027, 1, 15, hour, 0, tzinfo=tz).timestamp()
    except Exception:  # pragma: no cover - no tzdata: fall back to system local time
        import time as _t

        return _t.mktime((2027, 1, 15, hour, 0, 0, 0, 0, -1))


NOW_9AM = at_hour(9)


class _FakeSpotify:
    """Stands in for the Web API. Only implements endpoints that still exist."""

    def __init__(self):
        self.queued: list[str] = []
        self.created: list[str] = []
        self.searched: list[str] = []
        self.volume_set = None

    def me(self):
        return {"id": "calvin", "display_name": "Calvin", "product": "premium"}

    def top_artists(self, time_range="medium_term", limit=30):
        return [{"name": "Sauti Sol", "genres": ["afropop", "kenyan pop"]},
                {"name": "Burna Boy", "genres": ["afrobeats"]}]

    def top_tracks(self, time_range="medium_term", limit=30):
        return [{"name": "Suzanna", "album": {"release_date": "2019-05-01"}}]

    def recently_played(self, limit=50):
        return [{"name": "Last Last", "album": {"release_date": "2022-05-13"}}]

    def saved_tracks(self, limit=50):
        return [{"name": "Extravaganza", "album": {"release_date": "2020-01-01"}}]

    def search_track(self, q):
        self.searched.append(q)
        if "nonexistent" in q.lower():
            return None                        # unverifiable -> must never be queued
        explicit = "explicit" in q.lower()
        return {"uri": f"spotify:track:{abs(hash(q)) % 10**6}", "name": q.split(" - ")[-1],
                "artists": [{"name": q.split(" - ")[0]}], "explicit": explicit,
                "popularity": 50, "album": {"release_date": "2021-01-01"}}

    def search_artist(self, q):
        self.searched.append(q)
        if "madeup" in q.lower():
            return None
        return {"name": q, "uri": f"spotify:artist:{abs(hash(q)) % 10**6}"}

    def queue(self, uri):
        self.queued.append(uri)

    def create_playlist(self, name, description="", public=False):
        self.created.append(name)
        return {"id": "pl123", "name": name}

    def add_to_playlist(self, pid, uris):
        self.playlist_uris = uris

    def devices(self):
        return [{"id": "d1", "name": "Calvin's Laptop", "is_active": True},
                {"id": "d2", "name": "Phone", "is_active": False}]

    def transfer(self, device_id):
        self.transferred = device_id

    def play(self): self.played = True
    def pause(self): self.paused = True
    def next_track(self): self.skipped = True
    def previous_track(self): self.back = True
    def set_volume(self, p): self.volume_set = p
    def now_playing(self):
        return {"item": {"name": "Suzanna", "artists": [{"name": "Sauti Sol"}]}}


class _MusicLLM(LLMClient):
    def __init__(self, payload=None):
        self.routes = {"default": "m", "write": "m"}
        self.defaults = {}
        self._payload = payload

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        if self._payload is not None:
            return self._payload
        blob = " ".join(m["content"] for m in messages)
        if "order" in schema_hint:
            return {"order": [2, 0, 1], "intro": "Easing in with some Afrobeats."}
        if "adjacent" in blob.lower() or "artists" in schema_hint:
            return {"artists": [{"name": "Nviiri the Storyteller", "why": "same Nairobi soul lane"},
                                {"name": "Madeup Artist", "why": "does not exist"}]}
        return {"tracks": ["Sauti Sol - Suzanna", "Burna Boy - Last Last", "Nyashinski - Malaika"]}


@pytest.fixture
def music(mem, monkeypatch):
    import skills.music as mus

    engine = PersonaEngine(llm=None, memory=mem)
    monkeypatch.setattr(mus, "get_engine", lambda: engine)
    spoken: list[str] = []
    sp = _FakeSpotify()
    skill = MusicSkill(memory=mem, llm=_MusicLLM(), spotify=sp,
                       speak=lambda t: spoken.append(t), clock=lambda: NOW_9AM)
    mem.register_contract("music", ["music"], list(UNIVERSAL_INVARIANTS))
    return skill, sp, engine, spoken


# ================================================================= Spotify's real constraints
def test_client_defines_no_deprecated_endpoint_methods():
    """Recommendations/audio-features/related-artists are gone for new apps — don't pretend."""
    api = dir(SpotifyClient)
    for banned in ("recommendations", "audio_features", "audio_analysis", "related_artists",
                   "featured_playlists"):
        assert banned not in api


def test_client_refuses_a_deprecated_path_even_if_asked():
    c = SpotifyClient(session=object())
    c._token = lambda: "tok"                    # type: ignore[method-assign]
    for path in ("/recommendations?seed_artists=x", "/audio-features/123",
                 "/audio-analysis/4Yj", "/browse/featured-playlists?limit=5",
                 # the {id} in the pattern stands for a real id — the guard has to match one
                 "/artists/4YjuqjhwFOEbGZQwbMwLYm/related-artists"):
        with pytest.raises(SpotifyError, match="deprecated"):
            c._call("GET", path)


def test_deprecation_guard_does_not_overmatch():
    """`/artists/{id}/top-tracks` is alive and must not be caught by the related-artists rule."""
    assert _DEPRECATED_RE.match("/artists/4Yj/top-tracks") is None
    assert _DEPRECATED_RE.match("/me/top/tracks") is None
    assert _DEPRECATED_RE.match("/search?q=x") is None


def test_deprecated_list_is_documented():
    assert "/recommendations" in DEPRECATED_FOR_NEW_APPS
    assert "/audio-features" in DEPRECATED_FOR_NEW_APPS


# ================================================================= no active device (Phase 23)
def test_play_without_a_device_asks_the_laptop_to_open_spotify(music):
    """The Web API can't start the app, so 'open Spotify first' was a dead end by voice."""
    skill, sp, _, _ = music

    def no_device():
        raise SpotifyError("No active Spotify device — open Spotify somewhere first.")

    sp.play = no_device
    res = skill.play()
    assert res.data["client_actions"] == [{"op": "open", "app": "spotify"}]
    assert res.ok is False          # honest: playback did NOT start, we just cleared the blocker
    assert "starting it" in res.text.lower()


def test_other_spotify_errors_do_not_open_anything(music):
    """Only the one failure opening Spotify can actually fix should trigger it."""
    skill, sp, _, _ = music

    def not_premium():
        raise SpotifyError("Spotify returned 403 — the account likely isn't Premium.")

    sp.play = not_premium
    res = skill.play()
    assert "client_actions" not in res.data and res.ok is False


# ================================================================= first-run bootstrap
def test_connect_runs_the_oauth_flow_when_there_is_no_refresh_token(monkeypatch, capsys):
    """The bootstrap must not require the token it exists to produce.

    `SKILL.connect()` calls /me, which needs a refresh token — so with none set, `music connect`
    has to be the consent flow, not a health check.
    """
    import manage

    monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr("builtins.input",
                        lambda *_: "http://127.0.0.1:8888/callback?code=THE_CODE&state=x")
    exchanged: dict = {}

    def fake_exchange(self, code, redirect_uri):
        exchanged.update(code=code, redirect_uri=redirect_uri)
        return "THE_REFRESH_TOKEN"

    monkeypatch.setattr(SpotifyClient, "exchange_code", fake_exchange)
    rc = manage.cmd_music(SimpleNamespace(action="connect", cue=None,
                                          redirect="http://127.0.0.1:8888/callback"))
    out = capsys.readouterr().out
    assert rc == 0
    assert exchanged == {"code": "THE_CODE",              # parsed out of the pasted URL
                         "redirect_uri": "http://127.0.0.1:8888/callback"}
    assert "SPOTIFY_REFRESH_TOKEN=THE_REFRESH_TOKEN" in out
    assert "accounts.spotify.com/authorize" in out


def test_connect_dispatches_to_the_health_check_once_a_token_exists(monkeypatch):
    """With a token present, `connect` goes back to verifying the account, not re-consenting."""
    import manage

    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "already-have-one")
    monkeypatch.setattr(SpotifyClient, "exchange_code",
                        lambda *a, **k: pytest.fail("must not re-run the consent flow"))
    called: list[str] = []
    monkeypatch.setattr("skills.music.SKILL.connect",
                        lambda **k: called.append("connect") or CommandResult(text="ok"))
    rc = manage.cmd_music(SimpleNamespace(action="connect", cue=None, redirect="http://x/cb"))
    assert called == ["connect"] and rc == 0


def test_consent_flow_fails_before_asking_for_a_code_if_the_client_id_is_missing(monkeypatch,
                                                                                 capsys):
    """Don't hand out a URL with an empty client_id and only fail after they've approved it."""
    import manage

    monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must fail before prompting"))
    rc = manage.cmd_music(SimpleNamespace(action="connect", cue=None, redirect="http://x/cb"))
    assert rc == 1
    assert "SPOTIFY_CLIENT_ID is not set" in capsys.readouterr().out


def test_connect_flags_a_non_premium_account(music):
    skill, sp, _, _ = music
    sp.me = lambda: {"id": "c", "display_name": "Calvin", "product": "free"}
    res = skill.connect()
    assert res.ok is False and "Premium" in res.text


# ================================================================= taste model
def test_taste_model_uses_available_endpoints_only(music):
    skill, sp, engine, _ = music
    res = skill.taste()
    assert "afrobeats" in res.data["top_genres"]
    assert "Sauti Sol" in res.data["top_artists"]
    assert any(e in res.data["eras"] for e in ("2010s", "2020s"))


def test_taste_feeds_verified_persona_facts(music):
    skill, _, engine, _ = music
    skill.taste()
    facts = {f["key"]: f for f in engine.get_facts("music")}
    assert "top_genres" in facts and facts["top_genres"]["verified"] == 1
    assert facts["top_genres"]["source"] == "spotify"


# ================================================================= queueing (verified only)
def test_queue_resolves_every_track_via_search(music):
    skill, sp, _, _ = music
    skill.taste()
    res = skill.auto_queue(cue="study music", count=3)
    assert res.ok
    assert len(sp.queued) == len(res.data["queued"])
    assert sp.searched                          # nothing queued without a Search hit


def test_unverifiable_tracks_are_never_queued(music):
    skill, sp, _, _ = music
    skill._llm = _MusicLLM({"tracks": ["Nonexistent Band - Nonexistent Song"]})
    res = skill.auto_queue(cue="whatever")
    assert res.ok is False
    assert sp.queued == []
    assert "won't queue songs I can't verify" in res.text


# ================================================================= standing rules
def test_explicit_rule_filters_tracks_before_8am(mem, music):
    skill, sp, engine, _ = music
    engine.remember("never queue explicit lyrics before 8am", category="music")
    skill._now = lambda: at_hour(6)
    filters = skill.rule_filters("morning focus")
    assert filters["no_explicit"] is True

    skill._llm = _MusicLLM({"tracks": ["Someone - explicit banger", "Sauti Sol - Suzanna"]})
    res = skill.auto_queue(cue="morning focus", count=5)
    assert len(sp.queued) == 1                  # the explicit one was dropped
    assert "explicit filtered per your rule" in res.text


def test_explicit_rule_does_not_apply_after_the_cutoff(music):
    skill, _, engine, _ = music
    engine.remember("never queue explicit lyrics before 8am", category="music")
    skill._now = lambda: at_hour(9)
    assert skill.rule_filters("focus")["no_explicit"] is False


def test_unconditional_explicit_rule_applies_all_day(music):
    """No time bound -> it really does mean never."""
    skill, _, engine, _ = music
    engine.remember("never queue explicit lyrics", category="music")
    skill._now = lambda: at_hour(9)
    assert skill.rule_filters("focus")["no_explicit"] is True


def test_after_style_window(music):
    skill, _, engine, _ = music
    engine.remember("no explicit lyrics after 10pm", category="music")
    skill._now = lambda: at_hour(9)                    # 9am -> outside the window
    assert skill.rule_filters("x")["no_explicit"] is False
    skill._now = lambda: at_hour(23)                   # 11pm -> inside
    assert skill.rule_filters("x")["no_explicit"] is True


def test_context_bound_rule_only_applies_to_that_context(music):
    """'always start study sessions instrumental only' must not silence a workout mix."""
    skill, _, engine, _ = music
    engine.remember("always start study sessions instrumental only", category="music")
    assert skill.rule_filters("study music")["instrumental_only"] is True
    assert skill.rule_filters("workout")["instrumental_only"] is False


def test_two_rules_do_not_bleed_into_each_other(music):
    """A time bound on one rule must not leak onto another."""
    skill, _, engine, _ = music
    engine.remember("never queue explicit lyrics before 8am", category="music")
    engine.remember("always start study sessions instrumental only", category="music")
    skill._now = lambda: at_hour(9)                    # 9am: explicit rule is OUT of window
    f = skill.rule_filters("study music")
    assert f["no_explicit"] is False                   # the 8am bound stayed on its own rule
    assert f["instrumental_only"] is True


def test_music_skill_declares_the_music_category(music):
    """Phase 20 refused music rules because nothing read them — this closes that loop."""
    skill, _, _, _ = music
    assert skill.contract().reads_categories == ["music"]


# ================================================================= playlists
def test_playlist_created_on_calvins_own_account(music):
    skill, sp, _, _ = music
    skill.taste()
    res = skill.playlist(theme="late-night coding", count=3)
    assert res.ok and sp.created == ["late-night coding"]
    assert len(sp.playlist_uris) == 3


def test_playlist_needs_a_theme(music):
    skill, _, _, _ = music
    assert skill.playlist().ok is False


# ================================================================= discovery
def test_discovery_verifies_artists_and_never_claims_spotify_said_so(music):
    skill, sp, _, _ = music
    skill.taste()
    res = skill.discover()
    names = [a["name"] for a in res.data["artists"]]
    assert "Nviiri the Storyteller" in names
    assert "Madeup Artist" not in names        # unverifiable -> dropped
    assert "not Spotify's" in res.text          # framed honestly
    assert all(a["why"] for a in res.data["artists"])   # each has a one-line why


# ================================================================= DJ mode
def test_dj_sequences_and_says_it_is_not_mixing(music):
    skill, sp, _, spoken = music
    skill.taste()
    res = skill.dj(cue="evening wind-down", count=3)
    assert res.ok
    assert len(sp.queued) == 3
    assert "not audio mixing" in res.text       # honest about the API's limits
    assert "can't crossfade" in res.text


def test_dj_reorders_the_set(music):
    skill, sp, _, _ = music
    skill.taste()
    res = skill.dj(cue="build energy", count=3)
    # the fake LLM returns order [2,0,1]; the queue must follow that, not the original order
    assert res.data["queued"][0].endswith("Malaika")


def test_dj_narration_uses_the_voice_layer(music):
    skill, _, _, spoken = music
    skill.taste()
    skill.dj(cue="chill", count=2, narrate=True)
    assert spoken and "Easing in" in spoken[0]


def test_dj_narration_can_be_off(music):
    skill, _, _, spoken = music
    skill.taste()
    skill.dj(cue="chill", count=2, narrate=False)
    assert spoken == []


def test_no_voice_cloning_path_in_music(music):
    skill, _, _, _ = music
    assert "prebuilt_voices_only" in skill.contract().hard_invariants
    assert "never_claim_spotify_recommended" in skill.contract().hard_invariants


# ================================================================= transport (no approval gate)
def test_transport_controls(music):
    skill, sp, _, _ = music
    assert skill.play().ok and sp.played
    assert skill.pause().ok and sp.paused
    assert skill.next_track().ok and sp.skipped
    assert skill.previous_track().ok and sp.back
    assert skill.volume(percent=30).ok and sp.volume_set == 30


def test_device_transfer(music):
    skill, sp, _, _ = music
    res = skill.devices(transfer_to="phone")
    assert res.ok and sp.transferred == "d2"


def test_device_transfer_unknown(music):
    skill, _, _, _ = music
    assert skill.devices(transfer_to="fridge").ok is False


def test_now_playing(music):
    skill, _, _, _ = music
    assert "Suzanna" in skill.now_playing().text


def test_playback_errors_surface_cleanly(music):
    """A Spotify failure reaches Calvin in Spotify's own words rather than a stack trace.

    Uses a rate-limit error, not the no-device one: since Phase 23 the latter is handled
    specially (the laptop opens the app), so it no longer surfaces verbatim — see
    test_play_without_a_device_asks_the_laptop_to_open_spotify.
    """
    skill, sp, _, _ = music

    def boom():
        raise SpotifyError("Spotify 429: rate limited, try later.")

    sp.play = boom
    res = skill.play()
    assert res.ok is False and "rate limited" in res.text


def test_premium_detection_requests_the_scope_that_returns_product():
    """`product` only appears in /me when user-read-private was granted.

    Without that scope Spotify omits the field entirely, is_premium() sees None, and a real
    Premium account gets told "requires Premium" -- which is what Calvin hit: the CLI printed
    "Connected as Calvin (None)" and warned, on a genuinely Premium account.
    """
    from core.spotify import SCOPES

    assert "user-read-private" in SCOPES, (
        "without user-read-private, /me has no `product` and Premium can never be detected")


def test_is_premium_reads_the_product_field():
    from core.spotify import SpotifyClient

    c = SpotifyClient()
    c.me = lambda: {"display_name": "Calvin", "product": "premium"}
    assert c.is_premium() is True
    c.me = lambda: {"display_name": "Calvin", "product": "free"}
    assert c.is_premium() is False
    # the missing-scope case: absent field must not masquerade as a definite "free"
    c.me = lambda: {"display_name": "Calvin"}
    assert c.is_premium() is False


# ================================================================= continuous session (Phase 27)
def test_session_starts_queues_and_plays(music):
    skill, sp, _, _ = music
    res = skill.start_session(cue="afrobeats")
    assert res.data["session"] is True
    assert sp.queued, "nothing was queued to start the session"
    assert getattr(sp, "played", False) is True


def test_session_survives_the_laptop_and_keeps_topping_up(music):
    """The whole point of server-driven: the droplet tops the queue up on a timer."""
    skill, sp, _, _ = music
    skill.start_session(cue="focus")
    before = len(sp.queued)
    out = skill.session_tick()
    assert out.data["topped_up"] > 0
    assert len(sp.queued) > before


def test_tick_does_nothing_when_no_session_is_running(music):
    """A timer that acts unasked is how music starts by itself at 3am."""
    skill, sp, _, _ = music
    out = skill.session_tick()
    assert out.data["topped_up"] == 0
    assert sp.queued == []


def test_stop_ends_the_session_and_pauses(music):
    skill, sp, _, _ = music
    skill.start_session()
    res = skill.stop_session()
    assert res.data["session"] is False
    assert getattr(sp, "paused", False) is True
    # after stopping, the tick must not queue anything more
    sp.queued.clear()
    skill.session_tick()
    assert sp.queued == []


def test_stop_is_honest_that_queued_tracks_still_play(music):
    """Spotify has no clear-queue API. Claiming silence would be a small lie."""
    skill, _, _, _ = music
    skill.start_session()
    text = skill.stop_session().text.lower()
    assert "already-queued" in text or "may still play" in text


def test_session_status_reports_state(music):
    skill, _, _, _ = music
    assert skill.session_status().data["active"] is False
    skill.start_session(cue="deep house")
    st = skill.session_status()
    assert st.data["active"] is True and "deep house" in st.text


def test_a_device_that_disappears_does_not_kill_the_session(music):
    """Laptop closes mid-session: keep it alive so reopening Spotify resumes."""
    skill, sp, _, _ = music
    skill.start_session()

    def gone():
        raise SpotifyError("No active Spotify device — open Spotify somewhere first.")

    sp.now_playing = gone
    out = skill.session_tick()
    assert out.data["topped_up"] == 0
    assert skill.session_status().data["active"] is True, "session was killed by a sleeping laptop"


def test_starting_without_a_device_asks_the_laptop_to_open_spotify(music):
    skill, sp, _, _ = music
    sp.devices = lambda: []
    res = skill.start_session()
    assert res.ok is False and "open spotify" in res.text.lower()


def test_the_session_tick_is_scheduled(music):
    skill, _, _, _ = music
    ids = {j.id for j in skill.scheduled_jobs()}
    assert "music.session_tick" in ids, "nothing would ever top the queue up"
