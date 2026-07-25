import { describe, expect, it } from "vitest";
import { base64urlToBuffer, bufferToBase64url } from "./webauthnBrowser";

describe("webauthn base64url helpers", () => {
  it("round-trips arbitrary bytes, including ones needing padding", () => {
    for (const bytes of [
      [0, 1, 2, 3, 4],
      [255, 254, 253],
      [1],
      [1, 2],
      [1, 2, 3],
      [1, 2, 3, 4],
    ]) {
      const buf = new Uint8Array(bytes).buffer;
      const encoded = bufferToBase64url(buf);
      expect(encoded).not.toMatch(/[+/=]/); // base64url, not base64
      const decoded = new Uint8Array(base64urlToBuffer(encoded));
      expect(Array.from(decoded)).toEqual(bytes);
    }
  });

  it("decodes a known base64url challenge the same way py-webauthn would encode it", () => {
    // "hello" -> base64 "aGVsbG8=" -> base64url "aGVsbG8"
    const decoded = new Uint8Array(base64urlToBuffer("aGVsbG8"));
    expect(new TextDecoder().decode(decoded)).toBe("hello");
  });
});
