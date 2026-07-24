"""Shared mic-capture + barge-in logic for the desktop shells (Phase 24's tkinter window and
Phase 36's pywebview HUD). Extracted out of agent_window.py so the two shells share one copy
of this algorithm rather than risk two hand-written versions drifting apart — the adaptive
echo baseline it uses to tell "the assistant's own TTS in the mic" apart from "Calvin talking
over it" is subtle enough that a second copy is a correctness risk, not a convenience.

Takes its AssistantCore, frame queue, stream-liveness check AND timing constants as
arguments rather than importing voice_client's module-level constants directly: voice_client
imports `voice_utils` with a bare `from voice_utils import ...`, which only resolves when
client/ itself is on sys.path (how the two window scripts actually run), not when this module
is imported as the package `client.audio_io` (how pytest imports it). Callers that already
run in the "bare" context — agent_window.py, hud_window.py — pass voice_client's real
constants through; tests pass their own, with no import-path gymnastics either way.
"""

from __future__ import annotations

import queue
from collections import deque
from typing import Callable

try:  # bare (client/ on sys.path — how the window scripts actually run)
    from assistant_core import AssistantCore, MicState
    from voice_utils import is_silent, pcm_rms, silence_elapsed
except ImportError:  # package (project root on sys.path — how pytest imports this)
    from client.assistant_core import AssistantCore, MicState
    from client.voice_utils import is_silent, pcm_rms, silence_elapsed


def record_utterance_with_barge_in(
    core: AssistantCore,
    frames: "queue.Queue[bytes]",
    is_stream_open: Callable[[], bool],
    *,
    frame_ms: int = 30,
    end_silence_ms: int = 1800,
    max_utterance_seconds: float = 45.0,
    barge_min_rms: float = 700.0,
    barge_echo_ratio: float = 1.5,
) -> bytes | None:
    """Capture one turn, including natural barge-in while a response is playing.

    While TTS is audible, a short adaptive echo baseline is learned so the assistant's own
    speaker output doesn't immediately re-trigger it; a materially louder, sustained voice
    over that baseline interrupts playback. Returns `None` if the stream disappeared out
    from under us, `b""` if nothing was said, else raw PCM16 bytes.
    """
    out: list[bytes] = []
    pre_roll: deque[bytes] = deque(maxlen=max(1, int(300 / frame_ms)))
    silent = 0
    spoke = False
    echo_level = 0.0
    echo_frames = 0
    louder_frames = 0

    for _ in range(int(max_utterance_seconds * 1000 / frame_ms)):
        if not is_stream_open():
            return None
        try:
            f = frames.get(timeout=0.5)
        except queue.Empty:
            if not spoke:
                return b""
            continue
        level = pcm_rms(f)

        if core.state is MicState.SPEAKING and not spoke:
            pre_roll.append(f)
            echo_frames += 1
            if echo_frames <= max(1, int(300 / frame_ms)):
                echo_level = max(echo_level, level)
                continue
            trigger = max(barge_min_rms, echo_level * barge_echo_ratio)
            if level >= trigger:
                louder_frames += 1
                if louder_frames < max(2, int(120 / frame_ms)):
                    continue
                core.barge_in()
                out.extend(pre_roll)
                spoke = True
                silent = 0
            else:
                louder_frames = 0
                echo_level = max(level, echo_level * 0.97)
                continue

        out.append(f)
        if is_silent(f):
            silent += 1
            if spoke and silence_elapsed(silent, frame_ms=frame_ms, stop_after_ms=end_silence_ms):
                break
        else:
            if not spoke and core.state is MicState.THINKING:
                core.barge_in()
            spoke = True
            silent = 0
    return b"".join(out) if spoke else b""
