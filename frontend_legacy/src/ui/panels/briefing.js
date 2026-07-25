// Briefing (Phase 13). The real 07:00 briefing is a scheduled push (semester_planner.briefing
// fires via APScheduler and notifies), not an on-demand REST endpoint — so rather than fake a
// "Get briefing" button against a route that doesn't exist, this surfaces "what's due", which
// has a confirmed keyword route (core/intent.py's `whats_due` rule) and is the deadline half
// of the same data the briefing itself reads from.
import { sendCommand } from "../transcript.js";

let el, outputEl;

export function mount(container) {
  el = container;
  el.replaceChildren();
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = "What's due";
  btn.addEventListener("click", run);
  outputEl = document.createElement("div");
  outputEl.className = "panel-output dim";
  outputEl.textContent = "The 07:00 briefing arrives on its own — ask what's due any time.";
  el.append(btn, outputEl);
}

async function run() {
  outputEl.classList.remove("dim");
  outputEl.textContent = "Working on it…";
  try {
    const reply = await sendCommand("what's due");
    outputEl.textContent = reply.text;
  } catch (e) {
    outputEl.textContent = "⚠ " + e.message;
  }
}

// No session-driven state to reflect for this panel — kept for interface symmetry with the
// other panels, which are all rendered from the same session:update event.
export function render() {}
