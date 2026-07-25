import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./transport";

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

describe("transport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns parsed JSON on a 200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ok: true })));
    const result = await api.loginPassword("hunter2");
    expect(result).toEqual({ ok: true });
  });

  it("fetches job listings from /api/jobs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ jobs: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.jobs();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs");
    expect(result).toEqual({ jobs: [], total: 0 });
  });

  it("sends credentials same-origin and JSON body on POST", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await api.loginPassword("hunter2");
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/auth/password");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body)).toEqual({ password: "hunter2" });
  });

  it("throws ApiError with the server detail on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Incorrect password." }, { status: 401 })),
    );
    await expect(api.loginPassword("wrong")).rejects.toMatchObject({
      status: 401,
      message: "Incorrect password.",
    });
  });

  it("surfaces Retry-After on a 429", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "Too many attempts" }, { status: 429, headers: { "Retry-After": "12" } }),
      ),
    );
    try {
      await api.loginPassword("wrong");
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).retryAfter).toBe(12);
    }
  });

  it("falls back to statusText when the error body isn't JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("plain text", { status: 500, statusText: "Server Error" })),
    );
    await expect(api.session()).rejects.toMatchObject({ status: 500, message: "Server Error" });
  });
});
