// Browser-side passkey ceremonies (Slice 0c). Uses the modern
// `PublicKeyCredential.parseCreationOptionsFromJSON` / `.parseRequestOptionsFromJSON` /
// `credential.toJSON()` helpers (Chrome 116+, Safari 18+, Firefox 122+) so there is no
// manual base64url<->ArrayBuffer juggling here — the browser does it. Older browsers get an
// honest "not supported" error rather than a silent failure.
//
// §0: no biometric data ever reaches this file or the server behind it. `navigator.
// credentials.create()`/`.get()` talk to the platform authenticator (Face ID, Windows
// Hello, a security key) entirely inside the browser/OS; what comes back is a public key
// and a signature, never a template.
import { api } from "./transport.js";

export function supported() {
  return (
    typeof window !== "undefined" &&
    !!window.PublicKeyCredential &&
    typeof PublicKeyCredential.parseCreationOptionsFromJSON === "function" &&
    typeof PublicKeyCredential.parseRequestOptionsFromJSON === "function"
  );
}

export async function registerPasskey(label) {
  if (!supported()) throw new Error("This browser doesn't support passkeys.");
  const optionsJSON = await api("/api/auth/webauthn/register/options", {
    method: "POST",
    body: JSON.stringify({ label }),
  });
  const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(optionsJSON);
  const credential = await navigator.credentials.create({ publicKey });
  if (!credential) throw new Error("Passkey registration was cancelled.");
  return api("/api/auth/webauthn/register/verify", {
    method: "POST",
    body: JSON.stringify({ label, credential: credential.toJSON() }),
  });
}

export async function loginWithPasskey() {
  if (!supported()) throw new Error("This browser doesn't support passkeys.");
  // No session cookie needed for this call — you don't have one yet.
  const res = await fetch(
    `${location.protocol}//${location.host}/api/auth/webauthn/login/options`,
    { method: "POST", credentials: "same-origin" },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "No passkey registered — use the password instead.");
  }
  const optionsJSON = await res.json();
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(optionsJSON);
  const credential = await navigator.credentials.get({ publicKey });
  if (!credential) throw new Error("Passkey login was cancelled.");
  return api("/api/auth/webauthn/login/verify", {
    method: "POST",
    body: JSON.stringify({ credential: credential.toJSON() }),
  });
}
