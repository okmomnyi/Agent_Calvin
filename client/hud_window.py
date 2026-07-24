"""AgentOS desktop HUD (Phase 36 Slice 2) -- a pywebview shell over AssistantCore.

    python client/hud_window.py [--tray] [--compact]

Extends client/ rather than replacing it: the same AssistantCore (Phase 24) that
agent_window.py drives, the same Microphone/Transcriber/Speaker/send_to_agent/run_actions
helpers from voice_client.py, and the same barge-in recorder (audio_io.py, shared with
agent_window.py). Only the render target changes: frontend/ (Part A's one HUD codebase)
in a frameless always-on-top pywebview window, instead of a tkinter widget tree.

Two things worth knowing before touching this file:

1. Frontend files are served over a local loopback HTTP server, not opened via `file://`.
   pywebview's Windows backend is Chromium (WebView2), and Chromium blocks relative
   `<script type="module">` imports and same-origin `fetch()` assumptions under `file://` --
   frontend/src's ES modules and transport.js's same-origin API calls would silently break.
   A server bound to 127.0.0.1 on an ephemeral port keeps this genuinely LOCAL (every byte
   comes off this disk, nothing round-trips to the internet to fetch it) while giving the
   page the real http:// origin it needs, exactly like any other framework's dev server.

2. The bridge is a one-way trust boundary, same shape as Phase 23's desktop-app-control
   design: the page can ASK this process to do something (capabilities, resize), but every
   Bridge method validates its own inputs and returns {ok, error} -- it never raises into
   the page, and (starting Slice 4) it re-checks every request against this laptop's own
   allowlist rather than trusting the droplet.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import queue
import socketserver
import sys
import threading
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assistant_core import AssistantCore, MicState  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # the autostart scripts may provide variables directly
    pass

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

AGENT_WS_URL = os.getenv(
    "AGENT_WS_URL", f"ws://127.0.0.1:{os.getenv('AGENTOS_PORT', '8000')}/ws/voice")
HOTKEY = os.getenv("AGENT_HUD_HOTKEY", "<ctrl>+<alt>+j")
WAKE_WORD = os.getenv("AGENT_WAKE_WORD", "hey_jarvis")
# Same opt-in gate voice_client.py already uses (Phase 24 docs: "opt-in, never the default,
# because nobody opts out of a mic"). Set AGENT_CLIENT_MODE=voice to fold the wake word into
# this window instead of running voice_client.py's standalone loop.
WAKE_WORD_ENABLED = os.getenv("AGENT_CLIENT_MODE", "window") == "voice"

FULL_SIZE = (420, 640)
COMPACT_SIZE = (110, 110)
SCREEN_MARGIN = 24


def _http_base_from_ws(ws_url: str) -> str:
    """transport.js needs an http(s) base, not a ws(s) one -- derive it from the SAME env var
    everyone else authenticates against, so the HUD never points at a different server than
    the voice client / dashboard do."""
    for prefix, replacement in (("wss://", "https://"), ("ws://", "http://")):
        if ws_url.startswith(prefix):
            return replacement + ws_url[len(prefix):].split("/", 1)[0]
    return ws_url


SERVER_BASE = _http_base_from_ws(AGENT_WS_URL)


def _serve_frontend() -> int:
    """Bind an ephemeral loopback port serving frontend/ and return it. See module docstring
    point 1 for why this exists instead of a bare `file://` URL."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FRONTEND_DIR))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True, name="agentos-hud-static").start()
    return port


class Bridge:
    """Exposed to JS as `window.pywebview.api` (frontend/shells/desktop/bridge.js wraps it).

    Slice 2 scope only: capability reporting and window sizing. Slice 4+ adds
    openApp/closeApp/focusApp/openUrl/call/answer/hangup here, each re-validated against
    this laptop's own allowlist. Every method returns {ok, ...} / {ok:False, error} and
    never raises -- a JS-side await must never throw just because the laptop hiccupped.
    """

    def __init__(self, hud: "HudWindow") -> None:
        self._hud = hud
        self._adb = None

    @property
    def adb(self):
        if self._adb is None:
            from adb_bridge import AdbBridge

            self._adb = AdbBridge()
        return self._adb

    def capabilities(self) -> dict:
        # `apps` stays False until Phase 23's app-launch flow gets its own Slice 36 wiring.
        # `adb` stays False until Slice 5 wires the SERVER half (skills/phone.py, the
        # approval gate, the WSS path) — the bridge below is real as of Slice 4, but
        # reporting adb:True with nothing server-side able to use it yet would be exactly
        # the "fabricate a check that passed" §0 warns against; the phone panel would show
        # controls with no flow behind them.
        return {"adb": False, "apps": False, "mic": True}

    def set_compact(self, compact: bool) -> dict:
        try:
            self._hud.set_compact(bool(compact))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 - must never throw into the page
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------- phone (Slice 4)
    # Each method re-validates independently of whatever the server sent — this is the
    # laptop's own policy boundary, not a rubber stamp on the droplet's request.
    def call(self, number: str) -> dict:
        outcome = self.adb.call(str(number))
        return {"ok": outcome.ok, "detail": outcome.detail} if outcome.ok \
            else {"ok": False, "error": outcome.detail}

    def answer_call(self) -> dict:
        outcome = self.adb.answer()
        return {"ok": outcome.ok, "detail": outcome.detail} if outcome.ok \
            else {"ok": False, "error": outcome.detail}

    def hangup_call(self) -> dict:
        outcome = self.adb.hangup()
        return {"ok": outcome.ok, "detail": outcome.detail} if outcome.ok \
            else {"ok": False, "error": outcome.detail}

    def call_state(self) -> dict:
        state = self.adb.call_state()
        return {"ok": state is not None, "state": state}


class HudWindow:
    def __init__(self, *, start_compact: bool = False, tray: bool = False) -> None:
        import webview  # heavy optional dep; imported lazily like voice_client's audio libs

        self._webview = webview
        port = _serve_frontend()
        url = (f"http://127.0.0.1:{port}/index.html"
               f"?shell=desktop&server={quote(SERVER_BASE, safe='')}")

        self._compact = start_compact
        self._visible = True
        self._window = webview.create_window(
            "AgentOS", url, width=FULL_SIZE[0], height=FULL_SIZE[1],
            frameless=True, easy_drag=True, on_top=True, transparent=True,
            js_api=Bridge(self),
        )
        self._window.events.closed += self._on_closed

        self._mic_stream = None
        self._frames: "queue.Queue[bytes]" = queue.Queue()
        self.core = AssistantCore(
            recorder=self._record, open_mic=self._open_mic, close_mic=self._close_mic,
            transcribe=self._transcribe, send=self._send, speak=self._speak,
            interrupt_playback=self._stop_speaking, run_actions=self._run_actions,
            flush_input=self._flush, on_change=self._push_state,
        )

        self._wake_stop = threading.Event()
        self._wake_thread: threading.Thread | None = None
        self._tray_icon = None
        self._want_tray = tray
        self._hotkey_listener = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._want_tray:
            self._tray_icon = _build_tray(self)
        self._start_hotkey()
        if WAKE_WORD_ENABLED:
            self._wake_thread = threading.Thread(
                target=self._wake_loop, daemon=True, name="agentos-hud-wake")
            self._wake_thread.start()
        if self._compact:
            self.set_compact(True)
        self._webview.start()

    def _on_closed(self) -> None:
        self._wake_stop.set()
        self.core.shutdown()
        if self._hotkey_listener:
            self._hotkey_listener.stop()
        if self._tray_icon:
            self._tray_icon.stop()

    # ------------------------------------------------------------- window control
    def show(self) -> None:
        self._visible = True
        self._window.show()
        self._window.restore()

    def hide(self) -> None:
        """Hide, not close -- the tray icon (or the hotkey) is the way back in, mirroring
        agent_window.py's `_hide`. Closing the window is what actually shuts everything down."""
        self._visible = False
        self._window.hide()

    def toggle(self) -> None:
        (self.hide if self._visible else self.show)()

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        w, h = COMPACT_SIZE if compact else FULL_SIZE
        self._window.resize(w, h)
        self._reposition(w, h)
        # toggleAttribute, not a dataset string assignment -- `dataset.compact = ""` still
        # leaves the attribute PRESENT (any value, including empty string, satisfies the
        # [data-compact] CSS selector), so the "expand" half of this call would silently do
        # nothing visually.
        self._push_js(f"document.documentElement.toggleAttribute("
                       f"'data-compact', {str(compact).lower()});")

    def _reposition(self, w: int, h: int) -> None:
        try:
            import screeninfo

            screen = screeninfo.get_monitors()[0]
            sw, sh = screen.width, screen.height
        except Exception:  # noqa: BLE001 - best-effort; a wrong corner beats a crash
            sw, sh = 1920, 1080
        try:
            self._window.move(sw - w - SCREEN_MARGIN, sh - h - SCREEN_MARGIN)
        except Exception:  # noqa: BLE001 - not every pywebview backend supports move()
            pass

    # ------------------------------------------------------------- global hotkey
    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            print("pynput not installed -- global hotkey disabled (pip install pynput).")
            return
        try:
            self._hotkey_listener = keyboard.GlobalHotKeys({HOTKEY: self._on_hotkey})
            self._hotkey_listener.start()
        except Exception as exc:  # noqa: BLE001
            # Wayland (no portable global-hotkey API without a compositor-specific
            # extension) is the known gap here -- report it, don't crash the HUD over it.
            print(f"global hotkey '{HOTKEY}' unavailable: {exc}")

    def _on_hotkey(self) -> None:
        self.toggle()

    # ------------------------------------------------------------- state -> JS
    def _push_state(self) -> None:
        payload = {
            "state": self.core.state.value,
            "mic_on": self.core.mic_on,
            "turns": [{"who": t.who, "text": t.text, "actions": t.actions}
                      for t in self.core.turns[-40:]],
        }
        self._push_js(f"window.__agentosBridgeEvent && "
                      f"window.__agentosBridgeEvent({json.dumps(payload)});")

    def _push_js(self, script: str) -> None:
        try:
            self._window.evaluate_js(script)
        except Exception:  # noqa: BLE001 - page may not be ready yet; next change resyncs it
            pass

    # ------------------------------------------------------------- wake word (opt-in)
    def _wake_loop(self) -> None:
        """Only runs behind AGENT_CLIENT_MODE=voice. Opens its OWN mic stream to listen for
        the wake word, and closes it the instant a real session starts (`core.mic_on`) so it
        never competes with AssistantCore's own stream for the device."""
        try:
            from openwakeword.model import Model as WakeModel
        except ImportError:
            print("openwakeword not installed -- wake word disabled (see client/README.md).")
            return
        framework = os.getenv("AGENT_WAKE_FRAMEWORK", "onnx")
        wake = WakeModel(wakeword_models=[WAKE_WORD], inference_framework=framework)
        threshold = float(os.getenv("AGENT_WAKE_THRESHOLD", "0.5"))
        while not self._wake_stop.is_set():
            if self.core.mic_on:
                self._wake_stop.wait(0.3)
                continue
            self._wake_listen_once(wake, threshold)

    def _wake_listen_once(self, wake, threshold: float) -> None:
        import numpy as np
        import sounddevice as sd

        from voice_client import FRAME_SAMPLES, SAMPLE_RATE, WAKE_CHUNK

        q: "queue.Queue[bytes]" = queue.Queue()
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16", channels=1,
            callback=lambda data, *a: q.put(bytes(data)))
        stream.start()
        buf = np.empty(0, dtype=np.int16)
        try:
            while not self._wake_stop.is_set() and not self.core.mic_on:
                try:
                    f = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                buf = np.concatenate([buf, np.frombuffer(f, dtype=np.int16)])
                if len(buf) < WAKE_CHUNK:
                    continue
                chunk, buf = buf[:WAKE_CHUNK], buf[WAKE_CHUNK:]
                scores = wake.predict(chunk)
                if any(v > threshold for v in scores.values()):
                    self._on_wake()
                    return
        finally:
            stream.stop()
            stream.close()

    def _on_wake(self) -> None:
        self.show()
        self.set_compact(False)
        self.core.mic_on_()

    # ------------------------------------------------------------- audio (laptop-local)
    # Identical to agent_window.py's implementations -- same voice_client.py helpers, same
    # AssistantCore contract, just addressed as HudWindow methods instead of AgentWindow's.
    def _open_mic(self) -> None:
        import sounddevice as sd

        from voice_client import FRAME_SAMPLES, SAMPLE_RATE

        self._frames = queue.Queue()
        self._mic_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16", channels=1,
            callback=lambda data, *a: self._frames.put(bytes(data)))
        self._mic_stream.start()

    def _close_mic(self) -> None:
        stream, self._mic_stream = self._mic_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass

    def _flush(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def _record(self) -> bytes | None:
        from audio_io import record_utterance_with_barge_in
        from voice_client import END_SILENCE_MS, FRAME_MS, MAX_UTTERANCE_SECONDS

        return record_utterance_with_barge_in(
            self.core, self._frames, lambda: self._mic_stream is not None,
            frame_ms=FRAME_MS, end_silence_ms=END_SILENCE_MS,
            max_utterance_seconds=MAX_UTTERANCE_SECONDS,
            barge_min_rms=float(os.getenv("AGENT_BARGE_MIN_RMS", "700")),
            barge_echo_ratio=float(os.getenv("AGENT_BARGE_ECHO_RATIO", "1.5")))

    def _transcribe(self, pcm: bytes) -> str:
        from voice_client import Transcriber

        if not hasattr(self, "_stt"):
            self._stt = Transcriber()
        return self._stt.transcribe(pcm)

    def _speak(self, text: str, voice_id: str, rate: str) -> None:
        import asyncio

        from voice_client import Speaker

        if not hasattr(self, "_speaker"):
            self._speaker = Speaker()
        if not hasattr(self, "_loop"):
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(
            self._speaker.speak(text, voice_id, rate), self._loop).result(timeout=60)

    def _stop_speaking(self) -> None:
        speaker = getattr(self, "_speaker", None)
        if speaker is not None:
            speaker.stop()

    def _send(self, text: str) -> dict:
        import asyncio

        from voice_client import send_to_agent

        if not hasattr(self, "_loop"):
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(
            send_to_agent(text), self._loop).result(timeout=90)

    def _run_actions(self, actions: list) -> list:
        from voice_client import run_actions

        return run_actions(actions)


def _build_tray(hud: HudWindow):
    """Tray icon: the window lives here while hidden, mirroring agent_window.py's `_tray`."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    bg, teal = "#05070a", "#4fd8e8"
    img = Image.new("RGB", (64, 64), bg)
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 54, 54), outline=teal, width=3)
    d.ellipse((26, 26, 38, 38), fill=teal)
    icon = pystray.Icon(
        "agentos-hud", img, "AgentOS",
        menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: hud.show(), default=True),
            pystray.MenuItem("Hide", lambda: hud.hide()),
            pystray.MenuItem("Quit", lambda: hud._window.destroy())))
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main() -> int:
    if not os.getenv("AGENT_WS_TOKEN"):
        print("Set AGENT_WS_TOKEN first -- see client/README.md.")
        return 1
    hud = HudWindow(start_compact="--compact" in sys.argv, tray="--tray" in sys.argv)
    try:
        hud.start()
    finally:
        hud.core.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
