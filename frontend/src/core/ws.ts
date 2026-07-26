import { api } from "./transport";
import type { StageDirective } from "./stageTypes";

export type SocketStatus = "connecting" | "open" | "closed";

export interface VoiceMessage {
  ok: boolean;
  text: string;
  intent?: string;
  skill?: string;
  voice_id?: string;
  rate?: string;
  actions?: unknown[];
  // Phase 38: present only when core/presenter.py built one for this turn (or a catalyst
  // pushed one between turns) -- absent, not null, when nothing should change on the
  // stage. See kernel/app.py's ws_voice docstring on why the key is omitted rather than
  // sent as `directive: null`.
  directive?: StageDirective;
}

interface VoiceSocketHandlers {
  onMessage: (msg: VoiceMessage) => void;
  onStatusChange: (status: SocketStatus) => void;
}

const MAX_BACKOFF_MS = 15000;
const BASE_BACKOFF_MS = 500;

// Full jitter, capped: attempt 0 -> [250,500)ms, growing to a [7500,15000)ms ceiling. Ticket
// re-mint happens on every attempt (see connect()) since a ticket is single-use and only
// lives ~15s -- reusing one across reconnects would never work anyway.
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const capped = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt);
  return capped / 2 + random() * (capped / 2);
}

// One socket, reconnect-with-backoff, ticket minted fresh per attempt over REST (the only
// way a browser can attach the session cookie to /ws/voice -- see kernel/app.py's ws_voice
// docstring). A revoked/expired session surfaces as a 401 from wsTicket() or a 4401 close
// from the socket itself; either just backs off and retries, since the session cookie may
// still be refreshed by a login elsewhere (Telegram-independent, browser-tab-independent).
export class VoiceSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private handlers: VoiceSocketHandlers;

  constructor(handlers: VoiceSocketHandlers) {
    this.handlers = handlers;
  }

  async connect(): Promise<void> {
    if (this.stopped) return;
    this.handlers.onStatusChange("connecting");

    let ticket: string;
    try {
      ({ ticket } = await api.wsTicket());
    } catch {
      this.scheduleReconnect();
      return;
    }
    if (this.stopped) return;

    const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${wsProtocol}//${location.host}/ws/voice?ticket=${encodeURIComponent(ticket)}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempt = 0;
      this.handlers.onStatusChange("open");
    };
    ws.onmessage = (ev) => {
      try {
        this.handlers.onMessage(JSON.parse(ev.data));
      } catch {
        // malformed frame -- drop it, the socket itself is still fine
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return; // a newer socket already replaced this one
      this.handlers.onStatusChange("closed");
      if (!this.stopped) this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(this.attempt++);
    this.reconnectTimer = setTimeout(() => void this.connect(), delay);
  }

  send(text: string): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ text }));
    return true;
  }

  close(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }
}
