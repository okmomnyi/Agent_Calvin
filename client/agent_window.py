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
        self.log.tag_config("you", foreground=TEAL)
        self.log.tag_config("agent", foreground=INK)
        self.log.tag_config("system", foreground=AMBER, font=("Consolas", 8))

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
        for t in self.core.turns[-80:]:
            prefix = {"you": "you  ", "agent": "     ", "system": "  · "}[t.who]
            self.log.insert("end", f"{prefix}{t.text}\n\n", t.who)
        self.log.see("end")
        self.log.config(state="disabled")

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
        """One utterance: wait for speech, capture until it stops. None if the mic went away."""
        from voice_client import FRAME_MS, is_silent, silence_elapsed

        frames: list[bytes] = []
        silent = 0
        spoke = False
        for _ in range(int(15000 / FRAME_MS)):          # 15s ceiling
            if self._mic_stream is None:
                return None
            try:
                f = self._frames.get(timeout=0.5)
            except queue.Empty:
                if not spoke:
                    return b""                          # nothing said; loop round again
                continue
            frames.append(f)
            if is_silent(f):
                silent += 1
                if spoke and silence_elapsed(silent):
                    break
            else:
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
