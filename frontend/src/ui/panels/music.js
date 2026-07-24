// Music (Phase 22/27). Three buttons, each the exact literal phrase core/intent.py's
// music_start/music_stop/music_status keyword rules match — real actions, not decoration.
import { sendCommand } from "../transcript.js";

let el, outputEl;

export function mount(container) {
  el = container;
  el.replaceChildren();
  const row = document.createElement("div");
  row.className = "row";
  row.append(
    quickButton("Start", "start music"),
    quickButton("Stop", "stop the music"),
    quickButton("What's playing", "what's playing"),
  );
  outputEl = document.createElement("div");
  outputEl.className = "panel-output dim";
  outputEl.textContent = "Playback status appears here.";
  el.append(row, outputEl);
}

function quickButton(label, phrase) {
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = label;
  btn.addEventListener("click", async () => {
    outputEl.classList.remove("dim");
    outputEl.textContent = "…";
    try {
      const reply = await sendCommand(phrase);
      outputEl.textContent = reply.text;
    } catch (e) {
      outputEl.textContent = "⚠ " + e.message;
    }
  });
  return btn;
}

export function render() {}
