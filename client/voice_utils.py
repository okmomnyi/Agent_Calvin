"""Pure, hardware-free helpers for the AgentOS voice client.

Kept separate from voice_client.py (which imports heavy audio libraries) so this logic —
rate formatting, local command detection, silence/RMS detection, wake-word stripping —
is unit-testable without a microphone or the faster-whisper/edge-tts stack installed.
"""

from __future__ import annotations

import array
import math
import re

# Commands the client handles LOCALLY without a round-trip to the droplet.
_STOP_RE = re.compile(r"^\s*(stop|cancel|never ?mind|quiet|shush)\s*[.!]?\s*$", re.I)
_WAKE_PREFIXES = ("hey agent", "agent", "hey assistant", "ok agent")


def format_rate(percent: int) -> str:
    """edge-tts rate string: 0 -> '+0%', -10 -> '-10%', 15 -> '+15%'."""
    return f"{'+' if percent >= 0 else ''}{int(percent)}%"


def detect_local_command(transcript: str) -> str | None:
    """Return 'stop' for a local stop/cancel utterance, else None (send to server)."""
    if not transcript:
        return None
    if _STOP_RE.match(transcript.strip()):
        return "stop"
    return None


def strip_wake_word(transcript: str) -> str:
    """Remove a leading wake phrase so 'Hey Agent, check my email' -> 'check my email'."""
    t = transcript.strip()
    low = t.lower()
    for prefix in _WAKE_PREFIXES:
        if low.startswith(prefix):
            rest = t[len(prefix):]
            return rest.lstrip(" ,.:;-").strip()
    return t


def is_silent(pcm16: bytes, threshold: int = 500) -> bool:
    """True if a 16-bit PCM frame's RMS is below `threshold` (VAD fallback / end-of-speech).

    Computes RMS directly (stdlib `audioop` was removed in Python 3.13, PEP 594).
    """
    if not pcm16:
        return True
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) // 2 * 2])
    if not samples:
        return True
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return rms < threshold


def silence_elapsed(silent_frames: int, frame_ms: int = 30, stop_after_ms: int = 1200) -> bool:
    """True once trailing silence has lasted stop_after_ms (default 1.2s, per spec)."""
    return silent_frames * frame_ms >= stop_after_ms
