// Shared client-side state: the bearer token, connection status, the last /api/session
// payload, and negotiated capabilities. One place so panels don't each own a copy.
import { bus } from "./bus.js";

const TOKEN_KEY = "agentos_token";

const state = {
  token: (typeof localStorage !== "undefined" && localStorage.getItem(TOKEN_KEY)) || "",
  connected: false,
  session: null,
  capabilities: null,
};

export function getToken() {
  return state.token;
}

export function setToken(token) {
  state.token = token || "";
  if (state.token) localStorage.setItem(TOKEN_KEY, state.token);
  else localStorage.removeItem(TOKEN_KEY);
  bus.emit("session:token", state.token);
}

export function getState() {
  return state;
}

export function setConnected(ok) {
  if (state.connected === ok) return;
  state.connected = ok;
  bus.emit("session:connected", ok);
}

export function setSession(payload) {
  state.session = payload;
  bus.emit("session:update", payload);
}

export function setCapabilities(caps) {
  state.capabilities = caps;
  bus.emit("capabilities:update", caps);
}
