// Conversation log + typed input (A0). One channel shared by the free-text box and every
// panel's quick-action buttons, so a button press shows up in the same log a typed command
// would — there is exactly one intent router either way (docs/ARCHITECTURE.md §4).
import { bus } from "../core/bus.js";
import { command } from "../core/transport.js";
import { setState } from "./hud.js";

let listEl, inputEl, sendBtn;

export function mount({ list, input, send }) {
  listEl = list;
  inputEl = input;
  sendBtn = send;
  sendBtn.addEventListener("click", submitFromInput);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitFromInput();
  });
  bus.on("transport:message", (msg) => {
    if (msg && typeof msg.text === "string") append("them", msg.text, msg);
  });
}

// client/hud_window.py's AssistantCore (Phase 24) drives its own send/speak round trip for
// mic-originated turns — it doesn't go through transport.command(), so those turns arrive
// here as a snapshot via bridge:core (see boot.js) rather than a reply this module sent
// itself. `turns` is the last-40 snapshot every time; only the tail past what's already
// rendered is new.
let bridgeTurnCount = 0;

export function syncFromBridge(turns) {
  if (!Array.isArray(turns)) return;
  if (turns.length < bridgeTurnCount) bridgeTurnCount = 0; // a fresh session started over
  const fresh = turns.slice(bridgeTurnCount);
  bridgeTurnCount = turns.length;
  for (const t of fresh) {
    if (t.who === "system") continue; // app-action cards; the panels already show state
    append(t.who === "you" ? "you" : "them", t.text);
  }
}

function submitFromInput() {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  sendCommand(text);
}

// Exported so panels (jobs/briefing/music/study) can push a canned phrase through the exact
// same path a typed message takes, instead of inventing a second reply-rendering path.
export async function sendCommand(text, channel = "dashboard") {
  append("you", text);
  setState("thinking");
  try {
    const reply = await command(text, channel);
    append("them", reply.text || "", reply);
    // requires_confirmation (email_agent.compose, phone.call, ...) is the visual expression
    // of §0 P3 — the HUD ring goes amber and STAYS there until the next reply, rather than
    // this being buried in a panel (A3).
    if (reply.data && reply.data.requires_confirmation) {
      setState("awaiting_approval");
      bus.emit("confirmation:pending", reply);
    } else {
      setState(reply.ok === false ? "error" : "idle");
    }
    bus.emit("reply", reply);
    return reply;
  } catch (e) {
    append("them", `⚠ ${e.message}`);
    setState("error", { caption: e.message });
    throw e;
  }
}

function append(role, text, meta = {}) {
  if (!listEl) return;
  const row = document.createElement("div");
  row.className = `turn turn-${role}`;
  const roleEl = document.createElement("span");
  roleEl.className = "turn-role";
  roleEl.textContent = role === "you" ? "›" : "«";
  const textEl = document.createElement("span");
  textEl.className = "turn-text";
  textEl.textContent = text;
  row.append(roleEl, textEl);
  if (meta.skill) {
    const tag = document.createElement("span");
    tag.className = "turn-skill dim mono";
    tag.textContent = meta.skill;
    row.append(tag);
  }
  listEl.appendChild(row);
  listEl.scrollTop = listEl.scrollHeight;
}
