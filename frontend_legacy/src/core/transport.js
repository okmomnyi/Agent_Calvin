// WSS + REST transport, cookie-session auth, auto-reconnect with backoff (A0, migrated 0b).
//
// REST hits the existing /api/* surface (kernel/app.py) — the browser attaches the httpOnly
// session cookie automatically on same-origin requests; nothing here ever touches the raw
// credential. The WebSocket rides /ws/voice, authenticated by a single-use ~15s ticket minted
// over REST (POST /api/auth/ws-ticket) and passed as ?ticket= on the handshake — the fix for
// the bug that started this: a browser cannot set an Authorization header on `new
// WebSocket()`, so the old static AGENT_WS_TOKEN had to ride in the URL as a long-lived,
// reusable value. A ticket is single-use and burned on first use, so replaying the URL (a
// browser history entry, a proxy log) buys an attacker nothing.
//
// If the socket is down, `send()` falls back to the REST /api/command endpoint so a message
// is never silently dropped; the reply just arrives as a normal promise instead of a bus event.
import { bus } from "./bus.js";
import { setConnected, setLoggedIn } from "./session.js";

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
  const res = await fetch(httpBase() + path, {
    ...opts,
    credentials: "same-origin",  // attach the httpOnly session cookie
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    setLoggedIn(false);
    throw new Error("Not logged in.");
  }
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// Liveness only, no session required — mirrors kernel/app.py's public/authed split.
export async function health() {
  const res = await fetch(httpBase() + "/api/health");
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

export async function login(password) {
  const res = await fetch(httpBase() + "/api/auth/password", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) throw new Error("Incorrect password.");
  setLoggedIn(true);
  return res.json();
}

export async function logout() {
  await fetch(httpBase() + "/api/auth/logout", { method: "POST", credentials: "same-origin" });
  setLoggedIn(false);
}

async function mintWsTicket() {
  const res = await fetch(httpBase() + "/api/auth/ws-ticket", {
    method: "POST",
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error("Could not get a WS ticket — log in first.");
  const body = await res.json();
  return body.ticket;
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

  async _open() {
    let ticket;
    try {
      ticket = await mintWsTicket();
    } catch {
      setConnected(false);
      if (this._wanted) this._scheduleReconnect();
      return;
    }
    let ws;
    try {
      ws = new WebSocket(`${wsBase()}/ws/voice?ticket=${encodeURIComponent(ticket)}`);
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
      // No token in the message anymore -- the ticket already authenticated the handshake,
      // and the socket is bound to that session for its whole lifetime.
      this._ws.send(JSON.stringify({ text, channel }));
      return Promise.resolve(null);
    }
    return command(text, channel);
  }
}

export const voiceSocket = new VoiceSocket();
