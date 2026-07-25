import { useAppStore } from "./store";
import type { HudState, Turn } from "./types";

// The desktop shell's OWN conversation, driven by the laptop's real microphone
// (client/assistant_core.py's AssistantCore) -- separate from the web dashboard's
// server-mediated chat (core/ws.ts's VoiceSocket). client/hud_window.py pushes state over
// `window.__agentosBridgeEvent`, defined here; wireDesktopBridge() is a no-op if that global
// is never called (e.g. in the web shell, or before pywebview's bridge is ready).
interface DesktopTurn {
  who: string; // "you" | "agent" | "system"
  text: string;
  actions: unknown[];
}

interface DesktopBridgeEvent {
  state: string; // MicState.value: off | listening | recording | thinking | speaking
  mic_on: boolean;
  turns: DesktopTurn[];
}

const MIC_STATE_TO_HUD_STATE: Record<string, HudState> = {
  off: "idle",
  listening: "listening",
  recording: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

// AssistantCore's transcript is one row per speaker turn ("you" then "agent"); the chat
// panel renders one row per EXCHANGE ({text, reply}), matching the web shell's shape -- pair
// them up rather than teaching ChatPanel a second turn format. A dangling "you" with no
// reply yet (mid-conversation) is shown with an empty reply rather than dropped.
function toChatTurns(turns: DesktopTurn[]): Turn[] {
  const out: Turn[] = [];
  let pendingUser: string | null = null;
  for (const t of turns) {
    if (t.who === "you") {
      if (pendingUser !== null) {
        out.push({ text: pendingUser, reply: "", channel: "voice", at: Date.now() / 1000, skill: null });
      }
      pendingUser = t.text;
    } else if (t.who === "agent") {
      out.push({
        text: pendingUser ?? "",
        reply: t.text,
        channel: "voice",
        at: Date.now() / 1000,
        skill: null,
      });
      pendingUser = null;
    }
    // "system" rows (errors/status) aren't a real exchange -- left out of the chat view.
  }
  if (pendingUser !== null) {
    out.push({ text: pendingUser, reply: "", channel: "voice", at: Date.now() / 1000, skill: null });
  }
  return out;
}

export function wireDesktopBridge(): void {
  const target = window as unknown as { __agentosBridgeEvent?: (e: DesktopBridgeEvent) => void };
  target.__agentosBridgeEvent = (event: DesktopBridgeEvent) => {
    const store = useAppStore.getState();
    store.setHudState(MIC_STATE_TO_HUD_STATE[event.state] ?? "idle");
    store.setMicOn(event.mic_on);
    store.setTurns(toChatTurns(event.turns));
  };
}

// Calls a method on window.pywebview.api (client/hud_window.py's Bridge) if the desktop
// shell's bridge is actually present. Returns null rather than throwing when it isn't --
// the same "ask the real bridge, degrade honestly" rule capabilities.ts follows.
export async function callBridge<T>(method: string, ...args: unknown[]): Promise<T | null> {
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  const fn = api?.[method] as ((...a: unknown[]) => Promise<T>) | undefined;
  if (!fn) return null;
  try {
    return await fn(...args);
  } catch {
    return null;
  }
}
