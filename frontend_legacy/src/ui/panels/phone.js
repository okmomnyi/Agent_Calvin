// Phonebook + call state (Phase 36). Local shell only: index.html gates this panel's
// container behind data-requires="local", so it never renders — let alone fails — in the
// web shell (A0).
import { bus } from "../../core/bus.js";
import { sendCommand } from "../transcript.js";

let el, inputEl, outputEl, confirmEl;

export function mount(container) {
  el = container;
  el.replaceChildren();

  const row = document.createElement("div");
  row.className = "row";
  inputEl = document.createElement("input");
  inputEl.placeholder = "Find a contact…";
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch();
  });
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = "Find";
  btn.addEventListener("click", runSearch);
  row.append(inputEl, btn);

  outputEl = document.createElement("div");
  outputEl.className = "panel-output dim";
  outputEl.textContent = "Search your phone book by name, or ask to call someone.";

  // The pending-call preview (§0 P3: full resolved name + number, explicit confirm/deny —
  // A3 says this must be unmissable, not buried; the amber HUD ring is the other half of
  // that, wired in transcript.js).
  confirmEl = document.createElement("div");
  confirmEl.hidden = true;

  el.append(row, outputEl, confirmEl);

  bus.on("confirmation:pending", (reply) => {
    const preview = reply.data && reply.data.call_preview;
    if (preview) renderConfirmation(preview);
  });
  bus.on("hud:state", ({ state }) => {
    // Any OTHER reply (including a resolved/cancelled call) clears a stale preview card.
    if (state !== "awaiting_approval") clearConfirmation();
  });
}

function renderConfirmation(preview) {
  confirmEl.hidden = false;
  confirmEl.replaceChildren();
  confirmEl.className = "approval-row";

  const what = document.createElement("span");
  what.className = "what";
  what.textContent = `Call ${preview.name} at ${preview.number}?`;

  const actions = document.createElement("span");
  actions.className = "row-actions";
  const confirm = button("Confirm call", () => {
    sendCommand("confirm call");
    clearConfirmation();
  });
  const cancel = button("Cancel", () => {
    sendCommand("cancel");
    clearConfirmation();
  });
  actions.append(confirm, cancel);

  confirmEl.append(what, actions);
}

function clearConfirmation() {
  confirmEl.hidden = true;
  confirmEl.replaceChildren();
}

function button(label, onClick) {
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}

async function runSearch() {
  const name = inputEl.value.trim();
  if (!name) return;
  outputEl.classList.remove("dim");
  outputEl.textContent = "…";
  try {
    const reply = await sendCommand(`find contact ${name}`);
    outputEl.textContent = reply.text;
  } catch (e) {
    outputEl.textContent = "⚠ " + e.message;
  }
}

// No session-driven state yet — active-call display will render here once the bridge pushes
// live call_state (client/adb_bridge.py's call_state()) through bridge:core.
export function render() {}
