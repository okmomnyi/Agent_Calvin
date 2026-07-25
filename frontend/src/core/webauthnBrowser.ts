// Minimal WebAuthn browser glue -- base64url <-> ArrayBuffer only, no external dependency.
// The server (py-webauthn's options_to_json_dict) already emits base64url strings for every
// buffer field in the options it sends, and expects the assertion encoded the same way back,
// so this is the full extent of what's needed to drive navigator.credentials.get() from JSON.
export function base64urlToBuffer(b64url: string): ArrayBuffer {
  const pad = "=".repeat((4 - (b64url.length % 4)) % 4);
  const base64 = (b64url + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const buf = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
  return buf.buffer;
}

export function bufferToBase64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let str = "";
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export class PasskeyUnavailableError extends Error {}

// Options come from POST /api/auth/webauthn/login/options -- see kernel/app.py. Returns the
// JSON-safe assertion shape the /verify endpoints expect (credential.rawId etc as base64url
// strings), never the raw PublicKeyCredential (which doesn't survive JSON.stringify).
export async function authenticateWithPasskey(
  optionsJson: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!("credentials" in navigator)) {
    throw new PasskeyUnavailableError("This browser doesn't support passkeys (WebAuthn).");
  }
  const allowCredentials = (
    (optionsJson.allowCredentials as Array<Record<string, unknown>>) ?? []
  ).map((c) => ({ ...c, id: base64urlToBuffer(c.id as string) }));

  const publicKey = {
    ...optionsJson,
    challenge: base64urlToBuffer(optionsJson.challenge as string),
    allowCredentials,
  } as PublicKeyCredentialRequestOptions;

  const credential = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!credential) throw new Error("Passkey ceremony was cancelled.");

  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      authenticatorData: bufferToBase64url(response.authenticatorData),
      signature: bufferToBase64url(response.signature),
      userHandle: response.userHandle ? bufferToBase64url(response.userHandle) : null,
    },
  };
}
