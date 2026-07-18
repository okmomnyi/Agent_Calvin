"""Music companion — Spotify control layer (Phase 22).

Honest about what the Web API can and can't do (see core/spotify.py):

  * Spotify's Recommendations / Audio-Features / Related-Artists endpoints are gone for new
    apps, so the taste model is built from what IS available — recently played, top
    tracks/artists over three ranges, and the saved library — and the actual song choices
    come from the model's own knowledge, then get RESOLVED to real tracks via Search. Nothing
    is suggested that wasn't verified to exist.
  * There is no BPM/key/energy data, and the API cannot mix audio at any tier. So "DJ mode"
    here is **smart sequencing plus narrated transitions**, not beatmatching — ordering a set
    for flow using metadata we can actually see (genre, era, listening patterns), with an
    optional spoken line between tracks through the Phase 7 voice layer.
  * Any transition line is spoken by a **stock edge-tts voice** — never a clone of Calvin's
    voice (§0 Principle 9).

Discovery is framed as AgentOS's suggestion with a one-line "why" — never presented as
Spotify's own recommendation, because it isn't.

Playback is personal, not an action taken in Calvin's name to anyone else, so §0 Principle 3
(approval gates) doesn't apply to play/pause/skip/queue.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from collections import Counter
from typing import Any, Callable

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.persona_store import get_engine
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.spotify import SpotifyClient, SpotifyError

log = get_logger("skills.music")

_TASTE_KV = "music.taste"
_SESSION_KV = "music.session"
# How many tracks to keep queued ahead. Small enough that a "stop" takes effect within a
# track or two -- Spotify has no API to clear a queue, so anything already queued WILL play.
SESSION_LOOKAHEAD = 4
TIME_RANGES = ("short_term", "medium_term", "long_term")

# Substring of core.spotify's 404 message. Matched rather than typed as its own exception
# because it's the one Spotify failure Phase 23 can actually FIX (by opening the app).
_NO_DEVICE = "No active Spotify device"

# "…before 8am" / "…after 10pm" in a standing rule.
_TIME_RE = re.compile(r"\b(before|after)\s+(\d{1,2})\s*(am|pm)?\b", re.I)


def _to_24h(hour: int, meridiem: str | None) -> int:
    m = (meridiem or "").lower()
    if m == "pm" and hour < 12:
        return hour + 12
    if m == "am" and hour == 12:
        return 0
    return hour


class MusicSkill(BaseSkill):
    name = "music"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 spotify: SpotifyClient | None = None,
                 speak: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._llm = llm
        self._sp = spotify
        self._speak = speak
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def sp(self) -> SpotifyClient:
        if self._sp is None:
            self._sp = SpotifyClient()
        return self._sp

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "connect": self.connect, "taste": self.taste, "queue": self.auto_queue,
            "playlist": self.playlist, "discover": self.discover, "dj": self.dj,
            "play": self.play, "pause": self.pause, "next": self.next_track,
            "previous": self.previous_track, "volume": self.volume, "devices": self.devices,
            "now_playing": self.now_playing,
            "start_session": self.start_session, "stop_session": self.stop_session,
            "session_status": self.session_status, "session_tick": self.session_tick,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [
            ScheduledJob(id="music.taste", func=self.taste, trigger="cron",
                         kwargs={"day_of_week": "sun", "hour": 4}),
            # The heartbeat that makes a session continuous. It no-ops unless one is running,
            # so this is cheap; 4 minutes is under the length of most tracks, so the queue
            # never actually runs dry between ticks.
            ScheduledJob(id="music.session_tick", func=self.session_tick, trigger="interval",
                         kwargs={"minutes": 4}),
        ]

    def contract(self) -> SkillContract:
        """Declares 'music', so rules like 'no explicit before 8am' finally have a reader."""
        return SkillContract(reads_categories=["music"],
                             hard_invariants=["prebuilt_voices_only",
                                              "never_claim_spotify_recommended"])

    # ------------------------------------------------------------- connect
    def connect(self, **_: Any) -> CommandResult:
        try:
            me = self.sp.me()
        except SpotifyError as exc:
            return CommandResult(text=f"Spotify not connected: {exc}", ok=False)
        premium = me.get("product") == "premium"
        msg = f"Connected as {me.get('display_name') or me.get('id')} ({me.get('product')})."
        if not premium:
            msg += ("\n⚠️ Playback control and this integration require Premium — Spotify "
                    "restricts new apps to Premium accounts.")
        return CommandResult(text=msg, ok=premium, data={"premium": premium})

    # ------------------------------------------------------------- taste model
    def taste(self, refresh: bool = True, **_: Any) -> CommandResult:
        """Build a taste picture from endpoints that still exist; feed it to the persona KB."""
        if not refresh:
            cached = self.mem.kv_get(_TASTE_KV)
            if cached:
                return CommandResult(text="(cached taste model)", data=json.loads(cached))
        try:
            artists: list[dict[str, Any]] = []
            for rng in TIME_RANGES:
                artists += self.sp.top_artists(time_range=rng)
            tracks = self.sp.top_tracks() + self.sp.recently_played() + self.sp.saved_tracks()
        except SpotifyError as exc:
            return CommandResult(text=f"Couldn't read your library: {exc}", ok=False)

        genres = Counter(g for a in artists for g in (a.get("genres") or []))
        names = Counter(a.get("name") for a in artists if a.get("name"))
        eras = Counter(str(t.get("album", {}).get("release_date", ""))[:3] + "0s"
                       for t in tracks if t.get("album", {}).get("release_date"))
        model = {
            "top_genres": [g for g, _ in genres.most_common(12)],
            "top_artists": [n for n, _ in names.most_common(15)],
            "eras": [e for e, _ in eras.most_common(4)],
            "sample_tracks": [t.get("name") for t in tracks[:20] if t.get("name")],
            "built_at": self._now(),
        }
        self.mem.kv_set(_TASTE_KV, json.dumps(model))
        self._feed_persona(model)
        return CommandResult(
            text=(f"🎧 Taste model: {', '.join(model['top_genres'][:5]) or '—'}\n"
                  f"Artists: {', '.join(model['top_artists'][:6]) or '—'}\n"
                  f"Eras: {', '.join(model['eras']) or '—'}"),
            data=model)

    def _feed_persona(self, model: dict[str, Any]) -> None:
        """Real listening data -> verified persona facts (measured, not guessed)."""
        try:
            eng = get_engine()
            if model["top_genres"]:
                eng.add_fact("music", "top_genres", ", ".join(model["top_genres"][:8]),
                             source="spotify", verified=True)
            if model["top_artists"]:
                eng.add_fact("music", "top_artists", ", ".join(model["top_artists"][:10]),
                             source="spotify", verified=True)
            if model["eras"]:
                eng.add_fact("music", "eras", ", ".join(model["eras"]), source="spotify",
                             verified=True)
        except Exception:  # noqa: BLE001
            log.exception("feeding taste model to the persona KB failed")

    def _taste(self) -> dict[str, Any]:
        cached = self.mem.kv_get(_TASTE_KV)
        return json.loads(cached) if cached else {}

    # ------------------------------------------------------------- standing rules
    def _rules(self) -> list[str]:
        try:
            return get_engine().instructions_for_skill("music")
        except Exception:  # noqa: BLE001
            return []

    def rule_filters(self, cue: str = "") -> dict[str, bool]:
        """Turn Calvin's music rules into concrete filters for RIGHT NOW.

        Each rule is evaluated on its own — joining them would mix their semantics (a time
        bound from one rule must not leak onto another). A rule applies only if both its
        time window and its context (if any) match.
        """
        hour = self._local_hour()
        cue_l = (cue or "").lower()
        filters = {"no_explicit": False, "instrumental_only": False}

        for rule in self._rules():
            r = rule.lower()
            if not self._window_matches(r, hour) or not self._context_matches(r, cue_l):
                continue
            if "explicit" in r:
                filters["no_explicit"] = True
            if "instrumental" in r:
                filters["instrumental_only"] = True
        return filters

    def _local_hour(self) -> int:
        """The hour in CALVIN's timezone, not the server's.

        A rule like "before 8am" means 8am where he is. The droplet may well run UTC, which
        would fire the rule three hours out for Africa/Nairobi.
        """
        try:
            from zoneinfo import ZoneInfo

            return datetime.fromtimestamp(self._now(), ZoneInfo(get_settings().tz)).hour
        except Exception:  # noqa: BLE001 - missing tzdata -> fall back to system local time
            return time.localtime(self._now()).tm_hour

    @staticmethod
    def _window_matches(rule: str, hour: int) -> bool:
        """'…before 8am' applies only before 08:00; '…after 10pm' only after 22:00.

        A rule with no time bound applies all day — but a bounded rule must NOT be treated
        as unconditional just because it also says "never".
        """
        m = _TIME_RE.search(rule)
        if not m:
            return True
        bound = _to_24h(int(m.group(2)), m.group(3))
        return hour < bound if m.group(1) == "before" else hour >= bound

    @staticmethod
    def _context_matches(rule: str, cue: str) -> bool:
        """'…for study sessions' applies only when the cue is about studying."""
        for context in ("study", "workout", "commute", "coding", "focus"):
            if context in rule:
                return context in cue
        return True

    def _apply_filters(self, tracks: list[dict[str, Any]], filters: dict[str, bool]
                       ) -> tuple[list[dict[str, Any]], int]:
        kept, dropped = [], 0
        for t in tracks:
            if filters.get("no_explicit") and t.get("explicit"):
                dropped += 1
                continue
            kept.append(t)
        return kept, dropped

    # ------------------------------------------------------------- candidate generation
    def _candidates(self, cue: str, n: int = 10) -> list[str]:
        """Ask the model for track ideas from the taste picture — NOT Spotify's recommender."""
        model = self._taste()
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Suggest real, existing songs that fit the listener's taste and the cue. "
                    "Return JSON: tracks = ['Artist - Title', ...]. Use your own music knowledge; "
                    "do not invent songs that don't exist."},
                 {"role": "user", "content":
                    f"Cue: {cue or 'keep it going'}\nGenres: {model.get('top_genres')}\n"
                    f"Artists: {model.get('top_artists')}\nEras: {model.get('eras')}\n"
                    f"Give {n} tracks."}],
                schema_hint='{"tracks": [string]}', temperature=0.7, max_tokens=500)
            return [t for t in data.get("tracks", []) if isinstance(t, str)][:n]
        except LLMError:
            return [a for a in (self._taste().get("top_artists") or [])[:n]]

    def _resolve(self, queries: list[str], filters: dict[str, bool]) -> list[dict[str, Any]]:
        """Every idea must resolve to a REAL Spotify track before it goes anywhere near a queue."""
        found = []
        for q in queries:
            try:
                track = self.sp.search_track(q)
            except SpotifyError:
                continue
            if track:
                found.append(track)
        found, _ = self._apply_filters(found, filters)
        return found

    # ------------------------------------------------------------- auto-queue
    def auto_queue(self, cue: str = "", count: int = 8, **_: Any) -> CommandResult:
        filters = self.rule_filters(cue)
        tracks = self._resolve(self._candidates(cue, count * 2), filters)[:count]
        if not tracks:
            return CommandResult(text="Couldn't find anything that fits (and I won't queue "
                                      "songs I can't verify exist).", ok=False)
        queued = []
        for t in tracks:
            try:
                self.sp.queue(t["uri"])
                queued.append(f"{t['artists'][0]['name']} — {t['name']}")
            except SpotifyError as exc:
                return CommandResult(text=f"Queued {len(queued)} then hit: {exc}",
                                     ok=False, data={"queued": queued})
        note = " (explicit filtered per your rule)" if filters["no_explicit"] else ""
        return CommandResult(text=f"▶️ Queued {len(queued)} track(s){note}:\n" +
                                  "\n".join(f"  • {q}" for q in queued),
                             data={"queued": queued, "filters": filters})

    # ------------------------------------------------------------- continuous session
    def start_session(self, cue: str = "", **_: Any) -> CommandResult:
        """Keep music going until told to stop, driven from the SERVER.

        The point is that it survives Calvin's laptop sleeping: the droplet holds the session
        and tops the queue up on a timer, so playback continues on whichever Spotify device is
        active. The droplet has no speakers and is not a playback device -- it is the DJ, not
        the stereo.
        """
        cue = (cue or "").strip()
        try:
            devices = self.sp.devices()
        except SpotifyError as exc:
            return self._no_device(exc) if _NO_DEVICE in str(exc) else CommandResult(
                text=str(exc), ok=False)
        if not devices:
            return CommandResult(
                text="No active Spotify device. Open Spotify on your laptop or phone and play "
                     "anything for a second, then say 'start the session' again.", ok=False)

        self.mem.kv_set(_SESSION_KV, json.dumps({
            "active": True, "cue": cue, "started_at": self._now(),
            "last_topup": 0.0, "queued_total": 0}))
        first = self.auto_queue(cue=cue, count=SESSION_LOOKAHEAD)
        try:
            self.sp.play()
        except SpotifyError:
            pass                      # already playing is fine
        where = devices[0].get("name", "your device")
        return CommandResult(
            text=(f"🎵 Session started on {where}"
                  + (f" — {cue}" if cue else "") + ".\n"
                  "I'll keep the queue topped up until you say 'stop music'."),
            data={"session": True, "cue": cue, "device": where,
                  "queued": first.data.get("queued", [])})

    def stop_session(self, **_: Any) -> CommandResult:
        """End the session and pause. Honest about what Spotify cannot do."""
        raw = self.mem.kv_get(_SESSION_KV)
        was_active = bool(json.loads(raw).get("active")) if raw else False
        self.mem.kv_set(_SESSION_KV, json.dumps({"active": False, "stopped_at": self._now()}))
        paused = True
        try:
            self.sp.pause()
        except SpotifyError:
            paused = False
        if not was_active:
            return CommandResult(text="No session was running." +
                                      ("" if paused else " (Couldn't pause — nothing playing?)"))
        # The Web API has no "clear queue" call, so tracks already queued still exist. Saying
        # "stopped" while 4 more songs play would be a small lie.
        return CommandResult(
            text=("⏹ Session stopped — I won't queue anything more."
                  + ("" if paused else " (Couldn't pause playback.)")
                  + f"\nUp to {SESSION_LOOKAHEAD} already-queued track(s) may still play: "
                    "Spotify has no API to clear a queue, so skip them if you want silence."),
            data={"session": False})

    def session_status(self, **_: Any) -> CommandResult:
        raw = self.mem.kv_get(_SESSION_KV)
        state = json.loads(raw) if raw else {}
        if not state.get("active"):
            return CommandResult(text="🔇 No music session running.", data={"active": False})
        mins = int((self._now() - float(state.get("started_at", self._now()))) / 60)
        cue = state.get("cue") or "your taste"
        return CommandResult(
            text=f"🎵 Session running {mins} min — {cue}. "
                 f"{state.get('queued_total', 0)} track(s) queued so far. Say 'stop music' to end.",
            data={"active": True, **state})

    def session_tick(self, **_: Any) -> CommandResult:
        """Scheduled top-up. The thing that makes the session CONTINUOUS.

        Runs on the droplet, so it keeps working while the laptop is closed. Does nothing
        unless a session is active -- this is on a timer, and a timer that acts without being
        asked is how you get music starting by itself at 3am.
        """
        raw = self.mem.kv_get(_SESSION_KV)
        state = json.loads(raw) if raw else {}
        if not state.get("active"):
            return CommandResult(text="No session active.", data={"topped_up": 0})
        try:
            playing = self.sp.now_playing()
        except SpotifyError as exc:
            # Device went away (laptop closed, phone off). Keep the session ALIVE rather than
            # killing it: he'll reopen Spotify and expect it to resume.
            return CommandResult(text=f"Session paused — {exc}", data={"topped_up": 0}, ok=False)
        if not playing or not playing.get("item"):
            return CommandResult(text="Nothing playing right now; leaving the session alone.",
                                 data={"topped_up": 0})

        res = self.auto_queue(cue=state.get("cue", ""), count=SESSION_LOOKAHEAD)
        n = len(res.data.get("queued", []))
        state["last_topup"] = self._now()
        state["queued_total"] = int(state.get("queued_total", 0)) + n
        self.mem.kv_set(_SESSION_KV, json.dumps(state))
        return CommandResult(text=f"Topped up {n} track(s).", data={"topped_up": n})

    # ------------------------------------------------------------- playlists
    def playlist(self, theme: str = "", count: int = 20, **_: Any) -> CommandResult:
        if not theme:
            return CommandResult(text="Name a theme, e.g. 'late-night coding'.", ok=False)
        filters = self.rule_filters(theme)
        tracks = self._resolve(self._candidates(theme, count * 2), filters)[:count]
        if not tracks:
            return CommandResult(text="Couldn't assemble that playlist.", ok=False)
        try:
            pl = self.sp.create_playlist(theme, f"Built by AgentOS from your listening. {theme}.")
            self.sp.add_to_playlist(pl["id"], [t["uri"] for t in tracks])
        except SpotifyError as exc:
            return CommandResult(text=f"Couldn't create the playlist: {exc}", ok=False)
        return CommandResult(text=f"🎵 Created “{theme}” with {len(tracks)} track(s) — "
                                  f"edit it like any playlist.",
                             data={"playlist_id": pl.get("id"), "tracks": len(tracks)})

    # ------------------------------------------------------------- discovery
    def discover(self, count: int = 5, **_: Any) -> CommandResult:
        """Adjacent artists reasoned from genre/scene knowledge — Related Artists is gone."""
        model = self._taste()
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Suggest real artists adjacent to this listener's scene (genre/region/era). "
                    "For each give a one-line 'why'. Real artists only. Return JSON."},
                 {"role": "user", "content":
                    f"Artists: {model.get('top_artists')}\nGenres: {model.get('top_genres')}\n"
                    f"Give {count} adjacent artists."}],
                schema_hint='{"artists": [{"name": string, "why": string}]}',
                temperature=0.8, max_tokens=400)
        except LLMError:
            return CommandResult(text="Couldn't think of anything right now.", ok=False)

        out = []
        for a in data.get("artists", [])[:count]:
            name = str(a.get("name", "")).strip()
            if not name:
                continue
            try:
                hit = self.sp.search_artist(name)      # verify it's real before suggesting it
            except SpotifyError:
                continue
            if hit:
                out.append({"name": hit["name"], "why": str(a.get("why", "")).strip(),
                            "uri": hit.get("uri")})
        if not out:
            return CommandResult(text="Nothing I could verify on Spotify.", ok=False)
        lines = ["🔎 My suggestions (mine, not Spotify's — its recommender isn't available "
                 "to new apps):"]
        lines += [f"  • {a['name']} — {a['why']}" for a in out]
        return CommandResult(text="\n".join(lines), data={"artists": out})

    # ------------------------------------------------------------- DJ mode (sequencing)
    def dj(self, cue: str = "", count: int = 8, narrate: bool | None = None,
           **_: Any) -> CommandResult:
        """Order a set for flow and queue it. Sequencing + narration — NOT audio mixing."""
        filters = self.rule_filters(cue)
        tracks = self._resolve(self._candidates(cue, count * 2), filters)[:count]
        if not tracks:
            return CommandResult(text="Nothing to work with for that set.", ok=False)

        ordered, intro = self._sequence(tracks, cue)
        queued = []
        for t in ordered:
            try:
                self.sp.queue(t["uri"])
                queued.append(f"{t['artists'][0]['name']} — {t['name']}")
            except SpotifyError as exc:
                return CommandResult(text=f"Queued {len(queued)} then hit: {exc}", ok=False)

        if narrate is None:
            narrate = bool(get_settings().get("music", "dj_narration", default=True))
        if narrate and intro:
            self._say(intro)      # stock voice only — never a clone (§0 P9)
        return CommandResult(
            text=(f"🎚 DJ set queued ({len(queued)} tracks, sequenced for flow — this is "
                  f"ordering + narration, not audio mixing; the Web API can't crossfade):\n"
                  + "\n".join(f"  {i+1}. {q}" for i, q in enumerate(queued))
                  + (f"\n\n🎙 “{intro}”" if intro and narrate else "")),
            data={"queued": queued, "intro": intro if narrate else None})

    def _sequence(self, tracks: list[dict[str, Any]], cue: str) -> tuple[list[dict[str, Any]], str]:
        """Order by flow using metadata we can actually see (genre/era/popularity)."""
        listing = [f"{i}: {t['artists'][0]['name']} — {t['name']} "
                   f"({t.get('album', {}).get('release_date', '?')[:4]}, "
                   f"pop {t.get('popularity', '?')})" for i, t in enumerate(tracks)]
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Order these tracks into a set that flows: build or cool energy to suit the "
                    "cue and avoid jarring genre jumps. You have no tempo/key data — use genre, "
                    "era and popularity. Return JSON: order = [indices], intro = one short "
                    "spoken line to open the set."},
                 {"role": "user", "content": f"Cue: {cue}\n" + "\n".join(listing)}],
                schema_hint='{"order": [int], "intro": string}', temperature=0.5, max_tokens=300)
            order = [i for i in data.get("order", []) if isinstance(i, int) and 0 <= i < len(tracks)]
            seen: set[int] = set()
            order = [i for i in order if not (i in seen or seen.add(i))]
            order += [i for i in range(len(tracks)) if i not in seen]
            return [tracks[i] for i in order], str(data.get("intro", "")).strip()
        except LLMError:
            return tracks, ""

    def _say(self, text: str) -> None:
        if self._speak:
            self._speak(text)
            return
        log.info("DJ line (spoken by the laptop client in a stock voice): %s", text)

    # ------------------------------------------------------------- transport (no approval gate)
    def _simple(self, fn: Callable[[], None], ok_text: str) -> CommandResult:
        try:
            fn()
        except SpotifyError as exc:
            return self._no_device(exc) if _NO_DEVICE in str(exc) else CommandResult(
                text=str(exc), ok=False)
        return CommandResult(text=ok_text)

    @staticmethod
    def _no_device(exc: SpotifyError) -> CommandResult:
        """Spotify can't play to a device that isn't running — so ask the laptop to open it.

        The Web API has no way to start the app; "open Spotify somewhere first" was a dead end
        for a voice user with their hands full. Phase 23's client actions are the way out: the
        laptop opens Spotify, and Calvin says it again. `ok=False` stays honest — playback did
        NOT start, we only removed the reason it couldn't.
        """
        return CommandResult(
            text="Spotify wasn't open — starting it. Give it a second, then ask again.",
            data={"client_actions": [{"op": "open", "app": "spotify"}], "reason": str(exc)},
            ok=False)

    def play(self, **_: Any) -> CommandResult:
        return self._simple(self.sp.play, "▶️ Playing.")

    def pause(self, **_: Any) -> CommandResult:
        return self._simple(self.sp.pause, "⏸ Paused.")

    def next_track(self, **_: Any) -> CommandResult:
        return self._simple(self.sp.next_track, "⏭ Skipped.")

    def previous_track(self, **_: Any) -> CommandResult:
        return self._simple(self.sp.previous_track, "⏮ Back.")

    def volume(self, percent: int = 50, **_: Any) -> CommandResult:
        return self._simple(lambda: self.sp.set_volume(int(percent)), f"🔊 Volume {int(percent)}%.")

    def devices(self, transfer_to: str = "", **_: Any) -> CommandResult:
        try:
            devs = self.sp.devices()
            if transfer_to:
                match = next((d for d in devs if transfer_to.lower() in d["name"].lower()), None)
                if not match:
                    return CommandResult(text=f"No device matching '{transfer_to}'.", ok=False)
                self.sp.transfer(match["id"])
                return CommandResult(text=f"🔀 Playing on {match['name']}.")
        except SpotifyError as exc:
            return CommandResult(text=str(exc), ok=False)
        if not devs:
            return CommandResult(text="No Spotify Connect devices are active.", ok=False)
        return CommandResult(text="Devices:\n" + "\n".join(
            f"  • {d['name']}{' (active)' if d.get('is_active') else ''}" for d in devs),
            data={"devices": devs})

    def now_playing(self, **_: Any) -> CommandResult:
        try:
            cur = self.sp.now_playing()
        except SpotifyError as exc:
            return CommandResult(text=str(exc), ok=False)
        item = (cur or {}).get("item")
        if not item:
            return CommandResult(text="Nothing playing.")
        return CommandResult(text=f"🎶 {item['artists'][0]['name']} — {item['name']}",
                             data={"track": item.get("name")})


SKILL = MusicSkill()
