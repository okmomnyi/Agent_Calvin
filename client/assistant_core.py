"""Assistant session logic (Phase 24) -- the testable half of the desktop window.

Deliberately knows nothing about tkinter, pystray, or any widget, mirroring how
skills/telegram_bot.py keeps everything real in BaseCore and leaves the handlers as thin
wrappers. The window is a shell over this.

The rule this module exists to enforce:

    THE MICROPHONE IS CLOSED UNLESS CALVIN TURNED IT ON.

Not "ignored", not "muted in software" -- the OS audio stream is opened on mic-on and
CLOSED on mic-off, so Windows' own microphone indicator is the truth and the assistant
cannot listen without the operating system saying so. The previous design (a wake word,
autostarted, listening 24/7 with no visible state) failed that bar. Every §0 principle is
about explicit consent -- approval gates, never fabricate, no cloning -- and an always-on
mic sat badly beside them.

Everything external is injected (recorder, transcriber, sender, speaker, app controller),
so the whole session flow is testable with no audio hardware and no network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class MicState(str, Enum):
    OFF = "off"            # stream closed; OS shows no mic in use
    LISTENING = "listening"  # waiting for speech
    RECORDING = "recording"  # speech detected, capturing
    THINKING = "thinking"    # sent to the droplet, awaiting a reply
    SPEAKING = "speaking"    # playing the reply


@dataclass
class Turn:
    """One exchange, for the transcript pane."""
    who: str          # "you" | "agent" | "system"
    text: str
    actions: list = field(default_factory=list)


class AssistantCore:
    """Owns mic state and the session loop. No UI, no globals.

    recorder:    () -> bytes | None   -- blocks until an utterance is captured; None on stop
    open_mic:    () -> None           -- OPEN the OS audio stream
    close_mic:   () -> None           -- CLOSE it (must actually release the device)
    transcribe:  (bytes) -> str
    send:        (str) -> dict        -- the /ws/voice round trip
    speak:       (str, str, str) -> None
    run_actions: (list) -> list       -- Phase 23 desktop ops on this laptop
    """

    def __init__(self, *, recorder: Callable[[], bytes | None],
                 open_mic: Callable[[], None], close_mic: Callable[[], None],
                 transcribe: Callable[[bytes], str],
                 send: Callable[[str], dict],
                 speak: Callable[[str, str, str], None] | None = None,
                 interrupt_playback: Callable[[], None] | None = None,
                 run_actions: Callable[[list], list] | None = None,
                 flush_input: Callable[[], None] | None = None,
                 on_change: Callable[[], None] | None = None) -> None:
        self._recorder = recorder
        self._open_mic = open_mic
        self._close_mic = close_mic
        # Drop audio captured while we were NOT listening. The device keeps filling its buffer
        # through THINKING and SPEAKING, so without this you are always answering the previous
        # sentence -- and worse, the mic hears the agent's own TTS through the speakers and
        # dutifully transcribes it back. Calvin hit both: three "Hi Javis" in a row, each
        # answered late.
        self._flush_input = flush_input or (lambda: None)
        self._transcribe = transcribe
        self._send = send
        self._speak = speak
        self._interrupt_playback = interrupt_playback or (lambda: None)
        self._run_actions = run_actions
        self._on_change = on_change or (lambda: None)

        self.state = MicState.OFF
        self.turns: list[Turn] = []
        self.speak_replies = True
        self._mic_open = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._response_generation = 0

    # ------------------------------------------------------------- state
    @property
    def mic_on(self) -> bool:
        """Whether the DEVICE is open -- never inferred from the display state.

        This was `state is not MicState.OFF`, which is a different question. Typing with the
        mic off passes through THINKING, THINKING is not OFF, so the session ended in state
        LISTENING: the window would have shown "Listening..." with the microphone closed.
        The one claim this UI must never get wrong is whether it can hear you, so it is
        answered by the device and nothing else.
        """
        return self._mic_open

    @property
    def mic_device_open(self) -> bool:
        """True only while the OS stream is actually open. The claim the UI makes."""
        return self._mic_open

    def _set(self, state: MicState) -> None:
        self.state = state
        self._on_change()

    def _say(self, who: str, text: str, actions: list | None = None) -> None:
        self.turns.append(Turn(who, text, actions or []))
        self._on_change()

    # ------------------------------------------------------------- mic
    def toggle_mic(self) -> bool:
        """Flip the mic. Returns the new on/off. This is the consent boundary."""
        with self._lock:
            if self.mic_on:
                self.mic_off()
            else:
                self.mic_on_()
            return self.mic_on

    def mic_on_(self) -> None:
        with self._lock:
            if self.mic_on:
                return
            self._stop.clear()
            self._open_mic()
            self._mic_open = True
            self._flush_input()  # discard only audio from before consent was granted
            self._set(MicState.LISTENING)
            self._thread = threading.Thread(target=self._loop, daemon=True, name="agentos-mic")
            self._thread.start()

    def mic_off(self) -> None:
        """Stop listening and RELEASE the device. Safe to call when already off."""
        with self._lock:
            self._stop.set()
            if self._mic_open:
                # Closed before the thread is joined: the device must be released promptly,
                # and the loop is written to tolerate a stream disappearing underneath it.
                self._close_mic()
                self._mic_open = False
            self._set(MicState.OFF)

    def shutdown(self) -> None:
        """Window closed / quitting. Never leave the mic open behind us."""
        self.mic_off()
        with self._lock:
            self._response_generation += 1
            self._interrupt_playback()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)

    # ------------------------------------------------------------- session loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Capture continuously.  Responses run on separate workers, so speech during
                # THINKING/SPEAKING can interrupt instead of sitting in a device backlog.
                pcm = self._recorder()
            except Exception as exc:  # noqa: BLE001 - a mic glitch must not kill the session
                self._say("system", f"microphone error: {exc}")
                break
            if self._stop.is_set():
                break
            if not pcm:
                continue
            self._handle(pcm)
        # Whatever happened -- error, stop, exception -- the device does not stay open.
        with self._lock:
            if self._mic_open:
                self._close_mic()
                self._mic_open = False
            if self.state is not MicState.OFF:
                self._set(MicState.OFF)

    def _handle(self, pcm: bytes) -> None:
        self._set(MicState.RECORDING)
        try:
            text = (self._transcribe(pcm) or "").strip()
        except Exception as exc:  # noqa: BLE001
            self._say("system", f"transcription failed: {exc}")
            self._set(MicState.LISTENING)
            return
        if not text:
            self._set(MicState.LISTENING)
            return
        self.submit(text, _from_mic=True, _async=True)

    def barge_in(self) -> bool:
        """Cancel the current response as soon as new user speech is detected.

        The network request may already be in flight and cannot be unsent, but its generation
        becomes stale: its reply is neither spoken nor appended after the newer utterance.
        """
        with self._lock:
            if self.state not in (MicState.THINKING, MicState.SPEAKING):
                return False
            was_speaking = self.state is MicState.SPEAKING
            self._response_generation += 1
            if was_speaking:
                self._interrupt_playback()
            self._set(MicState.RECORDING)
            return True

    # ------------------------------------------------------------- submit (mic OR typed)
    def submit(self, text: str, *, _from_mic: bool = False, _async: bool = False) -> dict:
        """Send one command. Typing works with the mic off -- that's the point."""
        text = (text or "").strip()
        if not text:
            return {}
        with self._lock:
            # A new turn always wins over an older in-flight response.  This is replacement,
            # not a FIFO queue: stale replies are quietly dropped.
            self._response_generation += 1
            generation = self._response_generation
            if self.state is MicState.SPEAKING:
                self._interrupt_playback()
            self._say("you", text)
            self._set(MicState.THINKING)
        if _async:
            worker = threading.Thread(
                target=self._respond,
                args=(text, _from_mic, generation),
                daemon=True,
                name=f"agentos-response-{generation}",
            )
            worker.start()
            return {"in_flight": True}
        return self._respond(text, _from_mic, generation)

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._response_generation

    def _respond(self, text: str, from_mic: bool, generation: int) -> dict:
        """Complete one response; only the newest generation may reach the user."""
        try:
            reply = self._send(text)
        except Exception as exc:  # noqa: BLE001 - the droplet/tunnel drops constantly
            if self._is_current(generation):
                self._say("system", f"couldn't reach the agent: {exc}")
                self._set(MicState.LISTENING if self.mic_on else MicState.OFF)
            return {}

        if not self._is_current(generation):
            return {}

        body = reply.get("text", "")
        actions = reply.get("actions") or []

        if actions and self._run_actions:
            try:
                for outcome in self._run_actions(actions):
                    self._say("system", str(outcome))
            except Exception as exc:  # noqa: BLE001
                self._say("system", f"app action failed: {exc}")

        if body and self.speak_replies and self._speak and from_mic:
            self._set(MicState.SPEAKING)
            try:
                self._speak(body, reply.get("voice_id", "en-US-GuyNeural"),
                            reply.get("rate", "+0%"))
            except Exception as exc:  # noqa: BLE001
                if self._is_current(generation):
                    self._say("system", f"playback failed: {exc}")

        if not self._is_current(generation):
            return {}

        # Spoken turns appear after playback instead of printing a full answer and only then
        # beginning to talk. Typed turns still render immediately.
        self._say("agent", body, actions)
        self._set(MicState.LISTENING if self.mic_on else MicState.OFF)
        return reply

    # ------------------------------------------------------------- display
    def status_line(self) -> str:
        return {
            MicState.OFF: "Mic off - nothing is listening",
            # "just talk" is not filler. Calvin opened the window and said "Hi Javis" three
            # times, because every assistant he has met needs a wake word. This one does not,
            # and the UI should say so rather than let him guess.
            MicState.LISTENING: "Listening - just talk, no wake word",
            MicState.RECORDING: "Hearing you...",
            MicState.THINKING: "Thinking...",
            MicState.SPEAKING: "Speaking - talk over me to interrupt",
        }[self.state]
