// WSS + REST transport, bearer auth, auto-reconnect with backoff (A0).
//
// REST hits the existing /api/* surface (kernel/app.py) exactly as the old dashboard did.
// The WebSocket rides the existing /ws/voice channel, which already accepts plain text
// messages (not just audio-derived transcripts) and replies with {text, actions, voice_id,
// rate} — so it doubles as a push-capable command channel for the HUD. If the socket is
// down, `send()` falls back to the REST /api/command endpoint so a message is never
// silently dropped; the reply just arrives as a normal promise instead of a bus event.
import { bus } from "./bus.js";
import { getToken, setConnected } from "./session.js";

const MIN_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 20000;

// Desktop shell overrides this once it knows the droplet's URL (loaded from disk has no
// meaningful `location.host`). Web shell leaves it null and talks same-origin.
let serverBase = null;

export function setServerBase(url) {
  serverBase = url ? url.replace(/\/+$/, "") : null;
}

function httpBase() {
  if (serverBase) return serverBase;
  return `${location.protocol}//${location.host}`;
}

function wsBase() {
  if (serverBase) return serverBase.replace(/^http/, "ws");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

export async function api(path, opts = {}) {
  const token = getToken();
  const res = await fetch(httpBase() + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + token,
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) throw new Error("Unauthorized — check the access token.");
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// Liveness only, no token required — mirrors kernel/app.py's public/authed split.
export async function health() {
  const res = await fetch(httpBase() + "/api/health");
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

export function fetchSession() {
  return api("/api/session");
}

export function command(text, channel = "dashboard") {
  return api("/api/command", { method: "POST", body: JSON.stringify({ text, channel }) });
}

class VoiceSocket {
  constructor() {
    this._ws = null;
    this._backoff = MIN_BACKOFF_MS;
    this._wanted = false;
    this._reconnectTimer = null;
  }

  connect() {
    this._wanted = true;
    this._open();
  }

  disconnect() {
    this._wanted = false;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    if (this._ws) this._ws.close();
  }

  _open() {
    const token = getToken();
    if (!token) {
      setConnected(false);
      return;
    }
    let ws;
    try {
      ws = new WebSocket(`${wsBase()}/ws/voice?token=${encodeURIComponent(token)}`);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;
    ws.onopen = () => {
      this._backoff = MIN_BACKOFF_MS;
      setConnected(true);
      bus.emit("transport:open");
    };
    ws.onmessage = (evt) => {
      let data;
      try {
        data = JSON.parse(evt.data);
      } catch {
        return;
      }
      bus.emit("transport:message", data);
    };
    ws.onclose = () => {
      setConnected(false);
      bus.emit("transport:close");
      if (this._wanted) this._scheduleReconnect();
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    };
  }

  _scheduleReconnect() {
    const delay = this._backoff;
    this._backoff = Math.min(this._backoff * 2, MAX_BACKOFF_MS);
    this._reconnectTimer = setTimeout(() => {
      if (this._wanted) this._open();
    }, delay);
  }

  // Returns a promise that resolves once the message is on the wire (WS) or with the full
  // reply (REST fallback) — callers that need the reply either way should prefer `command()`
  // directly and treat this as best-effort push.
  send(text, channel = "voice") {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({ token: getToken(), text, channel }));
      return Promise.resolve(null);
    }
    return command(text, channel);
  }
}

export const voiceSocket = new VoiceSocket();
