"""Shared barge-in recorder (Phase 36 Slice 2), extracted from agent_window.py so the
tkinter window and the new pywebview HUD can't drift on this algorithm independently.

No audio hardware: frames are synthetic PCM16 bytes fed through a fake queue, and the
"AssistantCore" here is a minimal double exposing only what the function actually touches
(`.state`, `.barge_in()`) -- the same dependency-injection style as test_assistant_core.py.
"""

from __future__ import annotations

import array
import queue as queue_module

from client.assistant_core import MicState
from client.audio_io import record_utterance_with_barge_in

FRAME_MS = 10
END_SILENCE_MS = 30  # 3 silent frames at FRAME_MS=10


def _frame(sample: int, n: int = 8) -> bytes:
    return array.array("h", [sample] * n).tobytes()


LOUD = _frame(30000)
SILENT = _frame(0)


class _FakeCore:
    def __init__(self, state: MicState) -> None:
        self.state = state
        self.barge_in_calls = 0

    def barge_in(self) -> bool:
        self.barge_in_calls += 1
        return True


class _FrameQueue:
    """Not a real queue.Queue: `.get()` raises Empty immediately once drained, so a test
    exercising the "nothing said" path doesn't have to burn a real 0.5s timeout."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)

    def get(self, timeout: float | None = None) -> bytes:
        if not self._frames:
            raise queue_module.Empty
        return self._frames.pop(0)


def _record(core, frames, *, open_stream=True, **kw):
    return record_utterance_with_barge_in(
        core, _FrameQueue(frames), lambda: open_stream,
        frame_ms=FRAME_MS, end_silence_ms=END_SILENCE_MS,
        max_utterance_seconds=1.0, **kw)


def test_stream_closed_returns_none_without_touching_frames():
    core = _FakeCore(MicState.OFF)
    result = _record(core, [LOUD], open_stream=False)
    assert result is None
    assert core.barge_in_calls == 0


def test_nothing_said_returns_empty_bytes():
    core = _FakeCore(MicState.LISTENING)
    result = _record(core, [])  # queue empties immediately, spoke never becomes True
    assert result == b""
    assert core.barge_in_calls == 0


def test_normal_utterance_captures_until_trailing_silence():
    core = _FakeCore(MicState.LISTENING)
    frames = [LOUD, SILENT, SILENT, SILENT]  # 3 silent frames >= END_SILENCE_MS at FRAME_MS
    result = _record(core, frames)
    assert result == b"".join(frames)
    assert core.barge_in_calls == 0, "not speaking during playback -- no barge-in involved"


def test_quiet_speaker_echo_does_not_trigger_barge_in():
    """Frames at/under the learned echo baseline must never count as Calvin talking over it."""
    core = _FakeCore(MicState.SPEAKING)
    quiet_echo = _frame(200)  # below the default 700 RMS floor even before any baseline learning
    # Enough frames to run past the ~300ms echo-learning window, all at the same quiet level.
    frames = [quiet_echo] * 40
    result = _record(core, frames, barge_min_rms=700.0, barge_echo_ratio=1.5)
    assert result == b""
    assert core.barge_in_calls == 0


def test_a_materially_louder_voice_interrupts_playback():
    core = _FakeCore(MicState.SPEAKING)
    quiet_echo = _frame(200)
    # Baseline-learning frames, then a sustained loud voice clearly over the floor/ratio.
    frames = [quiet_echo] * 30 + [LOUD] * 20
    result = _record(core, frames, barge_min_rms=700.0, barge_echo_ratio=1.5)
    assert core.barge_in_calls == 1, "a sustained louder voice over playback must barge in exactly once"
    assert result != b""


def test_speech_during_thinking_also_barges_in():
    """Talking again before the reply has even started (state THINKING, not SPEAKING yet)."""
    core = _FakeCore(MicState.THINKING)
    frames = [LOUD, SILENT, SILENT, SILENT]
    result = _record(core, frames)
    assert core.barge_in_calls == 1
    assert result == b"".join(frames)
