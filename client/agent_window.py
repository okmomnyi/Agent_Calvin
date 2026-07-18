"""AgentOS desktop window (Phase 24) -- the assistant's face.

    python client/agent_window.py

A thin tkinter shell over AssistantCore, which holds all the logic (same split as
skills/telegram_bot.py: BotCore is real, the handlers are wrappers). Sits in the tray, mic
OFF, until you open it and decide otherwise.

Why this exists, replacing an always-on wake word:

  * Consent. Every §0 principle is about it -- approval gates, never fabricate, no cloning --
    and then a microphone listened 24/7 with no visible state. The mic device is now OPENED
    on toggle-on and CLOSED on toggle-off, so Windows' own mic indicator is the truth.
  * Reliability. A button is deterministic; "hey jarvis" is a probability that never fired
    on this laptop's quiet Realtek input.
  * Visibility. The client had no face, so when it crash-looped on a missing tflite runtime
    it did so silently for minutes; the log was the only UI and it was half NUL bytes. State
    you can see is worth more than state you have to grep for.

STT (faster-whisper) and TTS (stock edge-tts voices) both run locally -- your audio never
leaves the laptop; only the transcript does. That is the whole reason this is a native
window and not a browser page (the Web Speech API would ship your voice to a cloud).
"""

from __future__ import annotations

import asyncio
from collections import deque
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assistant_core import AssistantCore, MicState  # noqa: E402

# Console-charcoal palette, matching the PDFs (core/pdf.py) rather than a default grey dialog.
BG, PANEL, INK, DIM = "#1b1d21", "#24272c", "#e8e6e3", "#8b9096"
AMBER, TEAL, RED = "#d99a2b", "#3aa8a0", "#c0483c"


class AgentWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AgentOS")
        self.root.geometry("560x640")
        self.root.configure(bg=BG)
        self.root.minsize(420, 460)

        self._q: queue.Queue = queue.Queue()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._mic_stream = None

        self.core = AssistantCore(
            recorder=self._record, open_mic=self._open_mic, close_mic=self._close_mic,
            transcribe=self._transcribe, send=self._send, speak=self._speak,
            interrupt_playback=self._stop_speaking,
            run_actions=self._run_actions, flush_input=self._flush,
            # The core runs on a worker thread; tkinter is not thread-safe, so changes are
            # queued and drained on the UI thread. Touching widgets from the mic thread
            # deadlocks Tk in ways that look like random freezes.
            on_change=lambda: self._q.put("render"))

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self.root.after(80, self._drain)
        self._render()

    # ------------------------------------------------------------- widgets
    def _build(self) -> None:
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=14, pady=(12, 6))

        self.mic_btn = tk.Button(top, text="MIC OFF", width=12, relief="flat", bd=0,
                                 font=("Segoe UI", 11, "bold"), command=self._toggle,
                                 bg=PANEL, fg=DIM, activebackground=PANEL,
                                 activeforeground=INK, cursor="hand2")
        self.mic_btn.pack(side="left")

        self.status = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.status.pack(side="left", padx=10)

        self.link = tk.Label(top, text="", bg=BG, fg=DIM, font=("Consolas", 8))
        self.link.pack(side="right")

        self.log = scrolledtext.ScrolledText(
            self.root, bg=PANEL, fg=INK, insertbackground=INK, relief="flat", wrap="word",
            font=("Segoe UI", 10), padx=10, pady=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=14, pady=6)
        self.log.tag_config("you", foreground=TEAL, lmargin1=10, lmargin2=10)
        self.log.tag_config("agent", foreground=INK, lmargin1=10, lmargin2=10)
        self.log.tag_config("system", foreground=AMBER, font=("Consolas", 8))
        self.log.tag_config("speaker", foreground=DIM, font=("Segoe UI", 8, "bold"))
        # Chat-style attribution + indented body, rather than dividers between blocks: the
        # eye follows who-said-what far faster than it follows horizontal rules.
        self.log.tag_config("you_name", foreground=TEAL, font=("Segoe UI", 9, "bold"),
                            spacing1=6)
        self.log.tag_config("ai_name", foreground=AMBER, font=("Segoe UI", 9, "bold"),
                            spacing1=6)
        self.log.tag_config("card", foreground=DIM, font=("Consolas", 9),
                            lmargin1=10, lmargin2=22)
        self.log.tag_config("pending", foreground=DIM, font=("Segoe UI", 10, "italic"),
                            lmargin1=10)
        self.log.tag_config("heading", foreground=AMBER, font=("Segoe UI", 11, "bold"),
                            spacing1=5, spacing3=3)
        self.log.tag_config("bullet", foreground=INK, lmargin1=26, lmargin2=38)
        self.log.tag_config("meta", foreground=DIM, font=("Consolas", 9),
                            lmargin1=22, lmargin2=22)
        self.log.tag_config("link", foreground=TEAL, underline=True,
                            lmargin1=22, lmargin2=22)
        self.log.tag_config("divider", foreground="#3a3e45")

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=14, pady=(4, 12))
        self.entry = tk.Entry(bottom, bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                              font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._submit())
        tk.Button(bottom, text="Send", relief="flat", bd=0, command=self._submit,
                  bg=PANEL, fg=DIM, activebackground=PANEL, activeforeground=INK,
                  cursor="hand2").pack(side="right", ipadx=10, ipady=4)

        # Typing must work with the mic shut -- that is the point of the design.
        self.entry.focus_set()

    # ------------------------------------------------------------- ui actions
    def _toggle(self) -> None:
        self.core.toggle_mic()
        self._q.put("render")

    def _submit(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        threading.Thread(target=self.core.submit, args=(text,), daemon=True).start()

    def _hide(self) -> None:
        """Closing the window releases the mic. Never listen from behind a tray icon."""
        self.core.mic_off()
        self.root.withdraw()
        self._q.put("render")

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def quit(self) -> None:
        self.core.shutdown()
        self.root.quit()

    # ------------------------------------------------------------- render
    def _drain(self) -> None:
        dirty = False
        while True:
            try:
                self._q.get_nowait()
                dirty = True
            except queue.Empty:
                break
        if dirty:
            self._render()
        self.root.after(80, self._drain)

    def _render(self) -> None:
        on = self.core.mic_device_open
        self.mic_btn.config(text="MIC ON" if on else "MIC OFF",
                            fg=TEAL if on else DIM)
        colour = {MicState.OFF: DIM, MicState.LISTENING: TEAL, MicState.RECORDING: AMBER,
                  MicState.THINKING: AMBER, MicState.SPEAKING: TEAL}[self.core.state]
        self.status.config(text=self.core.status_line(), fg=colour)

        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        for turn in self.core.turns[-80:]:
            if turn.who == "system":
                # A tool-call card, not a chat message: what the agent DID, set apart from
                # what it SAID. Borrowed from OpenJarvis's ToolCallCard -- collapsing actions
                # into the prose is what made it read like a log file.
                self.log.insert("end", f"  ⚙  {turn.text}\n\n", "card")
                continue
            speaker = "You" if turn.who == "you" else "AgentOS"
            self.log.insert("end", f"{speaker}\n", "you_name" if turn.who == "you" else "ai_name")
            self._render_turn(turn.text, turn.who)
            if turn.actions:
                for a in turn.actions:
                    self.log.insert("end", f"  ⚙  {a.get('op','?')} {a.get('app','')}\n", "card")
            self.log.insert("end", "\n")

        # Live state at the foot of the transcript, so he can see it is working rather than
        # wondering whether it heard him (OpenJarvis's StreamingDots).
        if self.core.state is MicState.THINKING:
            self.log.insert("end", "AgentOS\n", "ai_name")
            self.log.insert("end", "  thinking…\n\n", "pending")
        elif self.core.state is MicState.RECORDING:
            self.log.insert("end", "  listening…\n\n", "pending")

        self.log.see("end")
        self.log.config(state="disabled")

    def _render_turn(self, text: str, who: str) -> None:
        """Render plain skill output as a readable hierarchy without trusting HTML/markdown."""
        for raw in (text or "").splitlines() or [""]:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                self.log.insert("end", "\n")
                continue
            if who == "system":
                tag = "system"
            elif stripped.startswith(("http://", "https://", "LINK   ")):
                tag = "link"
            elif stripped.startswith(("WHEN   ", "WHERE  ", "TAGS   ", "ID     ")):
                tag = "meta"
            elif stripped.startswith(("•", "- ")) or stripped[:2].rstrip(".").isdigit():
                tag = "bullet"
            elif stripped.isupper() or (len(stripped) < 70 and stripped.endswith(":")):
                tag = "heading"
            else:
                tag = who
            self.log.insert("end", stripped + "\n", tag)

    # ------------------------------------------------------------- audio (laptop-local)
    def _open_mic(self) -> None:
        import sounddevice as sd

        from voice_client import FRAME_SAMPLES, SAMPLE_RATE

        self._frames: queue.Queue = queue.Queue()
        self._mic_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16", channels=1,
            callback=lambda data, *a: self._frames.put(bytes(data)))
        self._mic_stream.start()

    def _close_mic(self) -> None:
        """Actually release the device, so the OS indicator goes out."""
        s, self._mic_stream = self._mic_stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass

    def _flush(self) -> None:
        """Throw away everything captured since we stopped listening.

        The stream fills continuously through THINKING and SPEAKING, so those frames are both
        stale and contaminated -- during SPEAKING the mic is literally recording the agent's
        own reply out of the speakers. Handing them to whisper produces the "queued audio"
        Calvin saw: every turn answering the previous sentence, plus the assistant talking to
        itself.
        """
        q = getattr(self, "_frames", None)
        if q is None:
            return
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def _record(self) -> bytes | None:
        """Capture one turn, including natural barge-in while a response is playing.

        During playback, a short adaptive echo baseline prevents the assistant's own speaker
        from immediately triggering itself. A materially louder overlapping voice interrupts
        playback; headphones remain the most reliable full-duplex setup.
        """
        from voice_client import END_SILENCE_MS, FRAME_MS, MAX_UTTERANCE_SECONDS
        from voice_utils import is_silent, pcm_rms, silence_elapsed

        frames: list[bytes] = []
        pre_roll = deque(maxlen=max(1, int(300 / FRAME_MS)))
        silent = 0
        spoke = False
        echo_level = 0.0
        echo_frames = 0
        louder_frames = 0
        barge_floor = float(os.getenv("AGENT_BARGE_MIN_RMS", "700"))
        barge_ratio = float(os.getenv("AGENT_BARGE_ECHO_RATIO", "1.5"))
        for _ in range(int(MAX_UTTERANCE_SECONDS * 1000 / FRAME_MS)):
            if self._mic_stream is None:
                return None
            try:
                f = self._frames.get(timeout=0.5)
            except queue.Empty:
                if not spoke:
                    return b""                          # nothing said; loop round again
                continue
            level = pcm_rms(f)

            # While TTS is audible, learn its mic echo and require a sustained, significantly
            # louder signal before treating it as the user talking over the assistant.
            if self.core.state is MicState.SPEAKING and not spoke:
                pre_roll.append(f)
                echo_frames += 1
                if echo_frames <= max(1, int(300 / FRAME_MS)):
                    echo_level = max(echo_level, level)
                    continue
                trigger = max(barge_floor, echo_level * barge_ratio)
                if level >= trigger:
                    louder_frames += 1
                    if louder_frames < max(2, int(120 / FRAME_MS)):
                        continue
                    self.core.barge_in()
                    frames.extend(pre_roll)
                    spoke = True
                    silent = 0
                else:
                    louder_frames = 0
                    echo_level = max(level, echo_level * 0.97)
                    continue

            frames.append(f)
            if is_silent(f):
                silent += 1
                if spoke and silence_elapsed(silent, stop_after_ms=END_SILENCE_MS):
                    break
            else:
                if not spoke and self.core.state is MicState.THINKING:
                    self.core.barge_in()
                spoke = True
                silent = 0
        return b"".join(frames) if spoke else b""

    def _transcribe(self, pcm: bytes) -> str:
        from voice_client import Transcriber

        if not hasattr(self, "_stt"):
            self._stt = Transcriber()          # loads whisper once, lazily
        return self._stt.transcribe(pcm)

    def _speak(self, text: str, voice_id: str, rate: str) -> None:
        from voice_client import Speaker

        if not hasattr(self, "_speaker"):
            self._speaker = Speaker()
        asyncio.run_coroutine_threadsafe(
            self._speaker.speak(text, voice_id, rate), self._loop).result(timeout=60)

    def _stop_speaking(self) -> None:
        speaker = getattr(self, "_speaker", None)
        if speaker is not None:
            speaker.stop()

    # ------------------------------------------------------------- server
    def _send(self, text: str) -> dict:
        from voice_client import send_to_agent

        return asyncio.run_coroutine_threadsafe(send_to_agent(text), self._loop).result(timeout=90)

    def _run_actions(self, actions: list) -> list:
        from voice_client import run_actions

        return run_actions(actions)


def _tray(win: AgentWindow):
    """Tray icon: the window lives here with the mic off until you want it."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    img = Image.new("RGB", (64, 64), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((18, 10, 46, 40), fill=TEAL)          # mic capsule
    d.rectangle((30, 40, 34, 50), fill=TEAL)        # stand
    d.rectangle((22, 50, 42, 54), fill=TEAL)        # base
    icon = pystray.Icon(
        "agentos", img, "AgentOS",
        menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: win.root.after(0, win.show), default=True),
            pystray.MenuItem("Quit", lambda: win.root.after(0, win.quit))))
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main() -> int:
    win = AgentWindow()
    tray = _tray(win)
    if "--tray" in sys.argv:
        win.root.withdraw()          # start hidden; the tray icon is the way in
    try:
        win.root.mainloop()
    finally:
        win.core.shutdown()          # never leave the mic open on the way out
        if tray:
            tray.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
