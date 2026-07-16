"""AgentOS laptop voice client (Phase 7).

Runs on Calvin's laptop: wake word (openwakeword) -> chime -> record until 1.2s silence
(sounddevice + RMS VAD) -> local faster-whisper STT -> authed WebSocket to the droplet's
/ws/voice -> spoken reply via edge-tts using the PRE-BUILT voice the server reports.
Push-to-talk fallback (hold a key) for noisy rooms; barge-in (wake word during playback
stops speech and listens). Voice cloning is out of scope permanently (§0 Principle 9):
this client only ever plays the stock edge-tts voice id the server sends back.

Heavy audio deps (faster-whisper, sounddevice, openwakeword, edge-tts) install from
client/requirements.txt and run on the laptop — not the droplet. See client/README.md.

Phase 19: this is a thin client with NO OS-specific hooks — mic capture plus a network call.
Push-to-talk uses stdin rather than a global hotkey (which would need root on Linux), so the
same install works identically on Ubuntu, any other Linux, or macOS. `--text` drops audio
entirely. Whatever the channel, the session lives on the VPS, so you can start here and
finish on Telegram or the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import tempfile
from pathlib import Path

from voice_utils import detect_local_command, is_silent, silence_elapsed, strip_wake_word

# ------------------------------------------------------------------ config
WS_URL = os.getenv("AGENT_WS_URL", "wss://agent.example.com/ws/voice")
WS_TOKEN = os.getenv("AGENT_WS_TOKEN", "")
WAKE_WORD = os.getenv("AGENT_WAKE_WORD", "hey_jarvis")  # openwakeword built-in model name
WHISPER_MODEL = os.getenv("AGENT_WHISPER_MODEL", "small")
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
PTT_KEY = os.getenv("AGENT_PTT_KEY", "ctrl+space")


def chime() -> None:
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ audio capture
class Microphone:
    """Streams 16kHz mono int16 frames from the default input device via sounddevice."""

    def __init__(self) -> None:
        import sounddevice as sd

        self._sd = sd
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16",
            channels=1, callback=self._cb)

    def _cb(self, indata, frames, time_info, status):  # noqa: ANN001
        self._q.get_nowait() if False else None
        self._q.put(bytes(indata))

    def __enter__(self):
        self._stream.start()
        return self

    def __exit__(self, *exc):
        self._stream.stop()
        self._stream.close()

    def frame(self, timeout: float = 1.0) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


def record_utterance(mic: Microphone, max_seconds: float = 15.0) -> bytes:
    """Record until 1.2s of trailing silence (or max_seconds). Returns raw PCM16 bytes."""
    frames: list[bytes] = []
    silent = 0
    spoke = False
    max_frames = int(max_seconds * 1000 / FRAME_MS)
    for _ in range(max_frames):
        f = mic.frame()
        if f is None:
            break
        frames.append(f)
        if is_silent(f):
            silent += 1
            if spoke and silence_elapsed(silent):
                break
        else:
            spoke = True
            silent = 0
    return b"".join(frames)


# ------------------------------------------------------------------ STT
class Transcriber:
    """Local faster-whisper (small, int8) — CPU is plenty fast for short utterances."""

    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def transcribe(self, pcm16: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm16, dtype=np.int16).astype("float32") / 32768.0
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=True)
        return " ".join(s.text for s in segments).strip()


# ------------------------------------------------------------------ TTS (pre-built voices only)
class Speaker:
    """Speaks text with edge-tts using the stock voice id the server reports. Barge-in aware."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def speak(self, text: str, voice_id: str, rate: str) -> None:
        import edge_tts

        if not text:
            return
        out = Path(tempfile.gettempdir()) / "agentos_tts.mp3"
        communicate = edge_tts.Communicate(text, voice_id, rate=rate)
        await communicate.save(str(out))
        await self._play(out)

    async def _play(self, path: Path) -> None:
        # Playback via a lightweight player; swap per-OS in README if needed.
        import sounddevice as sd
        import soundfile as sf

        data, sr = sf.read(str(path), dtype="float32")
        sd.play(data, sr)
        await asyncio.to_thread(sd.wait)

    def stop(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ server link
async def send_to_agent(transcript: str) -> dict:
    """Send a transcript over the authed WebSocket and return the JSON reply."""
    import websockets

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"token": WS_TOKEN, "text": transcript, "channel": "voice"}))
        return json.loads(await ws.recv())


# ------------------------------------------------------------------ desktop actions
_controller = None


def run_actions(actions) -> list:
    """Execute app ops the server asked for, against THIS machine's allowlist (Phase 23).

    The controller is built lazily and reused so apps.yaml is parsed once. Nothing here can
    raise into the voice loop: an unknown app is refused and printed, not thrown.
    """
    if not actions:
        return []
    global _controller
    if _controller is None:
        from client.apps import AppController

        _controller = AppController()
    outcomes = _controller.execute_all(actions)
    for outcome in outcomes:
        print(f"  [app] {outcome}")
    return outcomes


# ------------------------------------------------------------------ main loop
async def handle_utterance(pcm: bytes, stt: Transcriber, speaker: Speaker) -> None:
    transcript = strip_wake_word(stt.transcribe(pcm))
    if not transcript:
        return
    print(f"» {transcript}")
    if detect_local_command(transcript) == "stop":
        speaker.stop()
        return
    reply = await send_to_agent(transcript)
    text = reply.get("text", "")
    print(f"« {text}")
    # Apps first, then speak: "Opening Spotify" should be true by the time it's said, and the
    # launch is detached anyway so this costs milliseconds.
    run_actions(reply.get("actions"))
    await speaker.speak(text, reply.get("voice_id", "en-US-GuyNeural"), reply.get("rate", "+0%"))


async def wake_word_loop() -> None:
    from openwakeword.model import Model as WakeModel

    wake = WakeModel(wakeword_models=[WAKE_WORD])
    stt = Transcriber()
    speaker = Speaker()
    print(f"AgentOS voice client ready. Wake word: '{WAKE_WORD}'. (Ctrl+C to quit.)")
    with Microphone() as mic:
        while True:
            f = mic.frame()
            if f is None:
                continue
            import numpy as np

            scores = wake.predict(np.frombuffer(f, dtype=np.int16))
            if any(v > 0.5 for v in scores.values()):
                speaker.stop()  # barge-in: cut any current speech
                chime()
                pcm = record_utterance(mic)
                await handle_utterance(pcm, stt, speaker)


async def push_to_talk_loop() -> None:
    """Press Enter, speak, and it records until you stop talking.

    Deliberately uses stdin rather than a global hotkey: hotkey libraries need root on
    Linux and differ per OS, and this client must install identically on Ubuntu, any other
    Linux, or macOS (Phase 19). Enter works the same everywhere, with no privileges.
    """
    stt = Transcriber()
    speaker = Speaker()
    print("Push-to-talk. Press Enter to talk (Ctrl+C to quit).")
    with Microphone() as mic:
        while True:
            await asyncio.to_thread(input, "")     # blocks off the event loop
            speaker.stop()                          # barge-in
            chime()
            await handle_utterance(record_utterance(mic), stt, speaker)


async def text_loop() -> None:
    """No microphone at all — same kernel, same session, from any terminal."""
    print("Text mode. Type a command (Ctrl+C to quit).")
    while True:
        text = (await asyncio.to_thread(input, "› ")).strip()
        if not text:
            continue
        reply = await send_to_agent(text)
        print(reply.get("text", ""))
        run_actions(reply.get("actions"))       # --text drives apps too; same allowlist


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentOS laptop voice client")
    parser.add_argument("--ptt", action="store_true",
                        help="push-to-talk (press Enter) instead of the wake word")
    parser.add_argument("--text", action="store_true",
                        help="no microphone — plain text against the same session")
    args = parser.parse_args()
    if not WS_TOKEN:
        print("Set AGENT_WS_TOKEN (and optionally AGENT_WS_URL) first — see client/README.md.")
        return 1
    try:
        if args.text:
            asyncio.run(text_loop())
        else:
            asyncio.run(push_to_talk_loop() if args.ptt else wake_word_loop())
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
