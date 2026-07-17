"""Desktop assistant session logic (Phase 24).

The property that justifies this whole window existing:

    THE MIC DEVICE IS CLOSED UNLESS CALVIN TURNED IT ON.

Not muted, not ignored -- closed, so the OS indicator is the truth. Everything else here
(session flow, typing, actions, errors) matters less than that. The old design was a wake
word listening 24/7, autostarted, with no visible state; these tests exist so we can never
drift back to it by accident.

No audio hardware, no network: every dependency is injected.
"""

from __future__ import annotations

import threading

import pytest

from client.assistant_core import AssistantCore, MicState


class _Mic:
    """Fake OS audio device that records whether it is actually open."""

    def __init__(self, utterances=None):
        self.open_count = 0
        self.close_count = 0
        self.is_open = False
        self._utterances = list(utterances or [])
        self._served = threading.Event()

    def open(self):
        self.is_open = True
        self.open_count += 1

    def close(self):
        self.is_open = False
        self.close_count += 1

    def record(self):
        if self._utterances:
            return self._utterances.pop(0)
        self._served.set()
        # Block like a real recorder waiting for speech, so the loop parks here rather
        # than spinning; mic_off() interrupts by closing the device.
        threading.Event().wait(0.05)
        return None


def _core(mic=None, send=None, transcribe=None, speak=None, run_actions=None):
    mic = mic or _Mic()
    return AssistantCore(
        recorder=mic.record, open_mic=mic.open, close_mic=mic.close,
        transcribe=transcribe or (lambda pcm: "what's due this week"),
        send=send or (lambda t: {"text": "CAT 1 for CS305, in 3 days"}),
        speak=speak, run_actions=run_actions), mic


# ================================================================= the guarantee
def test_mic_starts_off_and_the_device_is_not_open():
    core, mic = _core()
    assert core.state is MicState.OFF
    assert core.mic_on is False
    assert core.mic_device_open is False
    assert mic.open_count == 0, "constructing the assistant must not touch the microphone"


def test_toggle_on_opens_the_device_and_off_closes_it():
    core, mic = _core()
    assert core.toggle_mic() is True
    assert mic.is_open is True and core.mic_device_open is True
    assert core.toggle_mic() is False
    assert mic.is_open is False and core.mic_device_open is False
    assert mic.close_count >= 1, "mic-off must RELEASE the device, not just ignore it"
    core.shutdown()


def test_shutdown_always_releases_the_device():
    """Closing the window with the mic on must not leave it open."""
    core, mic = _core()
    core.mic_on_()
    assert mic.is_open is True
    core.shutdown()
    assert mic.is_open is False
    assert core.state is MicState.OFF


def test_a_recorder_crash_still_releases_the_device():
    """The mic must not survive an exception in the session loop."""
    mic = _Mic()

    def boom():
        raise OSError("device disappeared")

    core = AssistantCore(recorder=boom, open_mic=mic.open, close_mic=mic.close,
                         transcribe=lambda p: "", send=lambda t: {})
    core.mic_on_()
    for _ in range(100):
        if not mic.is_open:
            break
        threading.Event().wait(0.02)
    assert mic.is_open is False, "a crash left the microphone open"
    assert core.state is MicState.OFF
    assert any("microphone error" in t.text for t in core.turns)
    core.shutdown()


def test_mic_off_is_idempotent():
    core, mic = _core()
    core.mic_off()
    core.mic_off()
    assert mic.close_count == 0        # never opened, nothing to close
    assert core.state is MicState.OFF


# ================================================================= typing (mic stays shut)
def test_typing_works_with_the_mic_off_and_never_opens_it():
    """The whole point: you can use it without ever being listened to."""
    sent = []
    core, mic = _core(send=lambda t: sent.append(t) or {"text": "CAT 1 for CS305"})
    core.submit("what's due this week")
    assert sent == ["what's due this week"]
    assert mic.open_count == 0 and core.mic_device_open is False
    assert [(t.who, t.text) for t in core.turns] == [
        ("you", "what's due this week"), ("agent", "CAT 1 for CS305")]


def test_typed_replies_are_not_spoken():
    """Speech is for a spoken conversation; typing should not talk back at you."""
    spoken = []
    core, _ = _core(speak=lambda *a: spoken.append(a))
    core.submit("what's due this week")
    assert spoken == []


def test_empty_submit_is_a_no_op():
    sent = []
    core, _ = _core(send=lambda t: sent.append(t) or {})
    core.submit("   ")
    assert sent == [] and core.turns == []


# ================================================================= session flow
def test_a_spoken_utterance_round_trips_and_is_spoken_back():
    spoken = []
    mic = _Mic(utterances=[b"\x01\x02"])
    core = AssistantCore(
        recorder=mic.record, open_mic=mic.open, close_mic=mic.close,
        transcribe=lambda pcm: "what's due this week",
        send=lambda t: {"text": "CAT 1 for CS305", "voice_id": "en-US-GuyNeural", "rate": "+0%"},
        speak=lambda *a: spoken.append(a))
    core.mic_on_()
    for _ in range(100):
        if spoken:
            break
        threading.Event().wait(0.02)
    core.shutdown()
    assert spoken and spoken[0][0] == "CAT 1 for CS305"
    assert spoken[0][1] == "en-US-GuyNeural", "must use the stock voice the server names (§0 P9)"
    assert ("you", "what's due this week") in [(t.who, t.text) for t in core.turns]


def test_desktop_actions_run_and_are_reported(monkeypatch):
    ran = []
    core, _ = _core(
        send=lambda t: {"text": "Opening spotify.", "actions": [{"op": "open", "app": "spotify"}]},
        run_actions=lambda acts: ran.extend(acts) or ["opened spotify"])
    core.submit("open spotify")
    assert ran == [{"op": "open", "app": "spotify"}]
    assert any(t.who == "system" and "opened spotify" in t.text for t in core.turns)


def test_a_failed_action_does_not_kill_the_session():
    def boom(acts):
        raise RuntimeError("spotify not installed")

    core, _ = _core(send=lambda t: {"text": "Opening spotify.",
                                    "actions": [{"op": "open", "app": "spotify"}]},
                    run_actions=boom)
    core.submit("open spotify")
    assert any("spotify not installed" in t.text for t in core.turns)
    assert core.state is MicState.OFF        # still alive, not wedged


# ================================================================= the network really does drop
def test_a_dropped_droplet_is_reported_not_raised():
    """Calvin's link flaps every few minutes; that must never take the window down."""
    def boom(t):
        raise ConnectionError("tunnel down")

    core, _ = _core(send=boom)
    core.submit("what's due this week")
    assert any("couldn't reach the agent" in t.text for t in core.turns)
    assert core.state is MicState.OFF


def test_a_drop_mid_session_keeps_the_mic_on():
    """A network blip is not a reason to silently stop listening."""
    def boom(t):
        raise ConnectionError("tunnel down")

    core, mic = _core(send=boom)
    core.mic_on_()
    core.submit("hello", _from_mic=True)
    assert core.mic_on is True
    assert core.state is MicState.LISTENING
    core.shutdown()


def test_transcription_failure_keeps_listening():
    def boom(pcm):
        raise RuntimeError("whisper exploded")

    mic = _Mic(utterances=[b"\x01"])
    core = AssistantCore(recorder=mic.record, open_mic=mic.open, close_mic=mic.close,
                         transcribe=boom, send=lambda t: {})
    core.mic_on_()
    for _ in range(100):
        if any("transcription failed" in t.text for t in core.turns):
            break
        threading.Event().wait(0.02)
    assert core.mic_on is True
    core.shutdown()


# ================================================================= status
@pytest.mark.parametrize("state,expect", [
    (MicState.OFF, "nothing is listening"),
    (MicState.LISTENING, "no wake word"),
    (MicState.THINKING, "Thinking"),
])
def test_status_line_tells_the_truth(state, expect):
    core, _ = _core()
    core.state = state
    assert expect in core.status_line()


def test_state_never_claims_to_listen_while_the_device_is_shut():
    """Regression: mic_on was derived from `state`, and THINKING is not OFF -- so typing with
    the mic closed ended in LISTENING and the window said "Listening..." with the mic shut.
    The single claim this UI must never get wrong."""
    core, mic = _core()
    for text in ("what's due", "and tomorrow", "thanks"):
        core.submit(text)
        assert core.state is MicState.OFF, f"claimed {core.state} after typing with mic off"
        assert core.mic_on is False
        assert mic.open_count == 0


# ================================================================= real-time, not a backlog
def test_audio_captured_while_busy_is_discarded_not_queued():
    """Calvin: "Its queing my audio instead of listening and responding in real time".

    The device buffer keeps filling through THINKING and SPEAKING. Without a flush, every
    turn answers the PREVIOUS sentence and the backlog grows -- he said "Hi Javis" three
    times and got three late replies. Worse, while speaking, the mic picks up the agent's
    own TTS through the speakers and transcribes it straight back.
    """
    flushes, order = [], []
    mic = _Mic(utterances=[b"\x01", b"\x02"])

    def record():
        order.append("record")
        return mic.record()

    core = AssistantCore(
        recorder=record, open_mic=mic.open, close_mic=mic.close,
        transcribe=lambda pcm: "hi",
        send=lambda t: {"text": "hello"},
        flush_input=lambda: (flushes.append(1), order.append("flush")))
    core.mic_on_()
    for _ in range(100):
        if len(flushes) >= 2:
            break
        threading.Event().wait(0.02)
    core.shutdown()
    assert flushes, "stale audio was never discarded -- the backlog is back"
    # every capture must be preceded by a flush, never the other way round
    assert order[0] == "flush"
    for a, b in zip(order, order[1:]):
        assert not (a == "record" and b == "record"), "two captures with no flush between"


def test_flush_is_optional():
    """A caller that passes no flush still works (it just keeps the old behaviour)."""
    core, _ = _core()
    core.submit("hello")
    assert any(t.who == "agent" for t in core.turns)
