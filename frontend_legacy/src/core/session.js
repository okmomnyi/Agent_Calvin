// Shared client-side state: connection status, the last /api/session payload, and
// negotiated capabilities. One place so panels don't each own a copy.
//
// Slice 0a/0b: there is no token here anymore, and never was a raw one after 0b — auth is
// an httpOnly Secure SameSite=Strict cookie the server sets, which JS cannot read and does
// not need to: the browser attaches it automatically on same-origin requests. What used to
// live here as `getToken()`/`setToken()` (a JS-readable secret in localStorage) is gone.
import { bus } from "./bus.js";

const state = {
  loggedIn: false,
  connected: false,
  session: null,
  capabilities: null,
};

export function getState() {
  return state;
}

export function setLoggedIn(ok) {
  if (state.loggedIn === ok) return;
  state.loggedIn = ok;
  bus.emit("session:loggedIn", ok);
}

export function isLoggedIn() {
  return state.loggedIn;
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
