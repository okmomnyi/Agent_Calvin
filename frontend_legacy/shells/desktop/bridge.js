// The one file in this codebase that touches `window.pywebview` directly (A0's "capability
// negotiation" principle: core/ and ui/ stay shell-agnostic). client/hud_window.py exposes a
// `Bridge` object as `window.pywebview.api`; this wraps it into a Promise-based, always-safe
// interface — a bridge call that throws (or doesn't exist yet, e.g. web shell) resolves to
// `{ok:false, error}` rather than rejecting, matching what the Python side itself guarantees
// (client/hud_window.py's Bridge methods never raise into the page either).
import { bus } from "../../src/core/bus.js";

export function available() {
  return typeof window !== "undefined"
    && typeof window.pywebview !== "undefined"
    && !!window.pywebview.api;
}

async function call(method, ...args) {
  if (!available() || typeof window.pywebview.api[method] !== "function") {
    return { ok: false, error: `desktop bridge has no method "${method}"` };
  }
  try {
    return await window.pywebview.api[method](...args);
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}

export const bridge = {
  available,
  capabilities: () => call("capabilities"),
  setCompact: (compact) => call("set_compact", !!compact),
};

// client/hud_window.py pushes AssistantCore state changes (Phase 24's mic/turn state
// machine) by evaluating `window.__agentosBridgeEvent(payload)` on the page. Re-emitting
// on the shared bus keeps hud.js/transcript.js ignorant of pywebview entirely — see
// frontend/src/ui/boot.js for the subscriber that maps this onto HUD states and turns.
window.__agentosBridgeEvent = (payload) => bus.emit("bridge:core", payload);
