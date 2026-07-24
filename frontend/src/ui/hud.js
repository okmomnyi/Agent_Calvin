// The HUD state machine (A3): idle -> listening -> thinking -> speaking -> awaiting_approval
// -> error. CSS ([data-state=...] in hud.css) owns the actual animation; this module just
// owns which state is current and the caption text, and publishes changes on the bus so
// wave.js and anything else can react without hud.js knowing about them.
import { bus } from "../core/bus.js";

const STATES = ["idle", "listening", "thinking", "speaking", "awaiting_approval", "error"];

const CAPTIONS = {
  idle: "Standing by",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  awaiting_approval: "Awaiting your approval",
  error: "Something went wrong",
};

let root = null;
let captionEl = null;
let current = "idle";
let flashTimer = null;

export function mount(el) {
  root = el;
  captionEl = el.querySelector(".hud-caption");
  setState("idle");
}

export function setState(state, meta = {}) {
  if (!STATES.includes(state)) state = "idle";
  const changed = state !== current;
  current = state;
  if (root) {
    root.dataset.state = state;
    if (changed) {
      // Brief flash on an actual state change only — never an ambient pulse at rest (design
      // rule shared with the rest of this codebase's UI conventions).
      root.classList.remove("flash");
      // eslint-disable-next-line no-unused-expressions
      root.offsetWidth; // force reflow so re-adding the class restarts the animation
      root.classList.add("flash");
      if (flashTimer) clearTimeout(flashTimer);
      flashTimer = setTimeout(() => root.classList.remove("flash"), 220);
    }
  }
  if (captionEl) captionEl.textContent = meta.caption || CAPTIONS[state];
  bus.emit("hud:state", { state, meta });
}

export function getState() {
  return current;
}
