import { describe, expect, it, beforeEach } from "vitest";
import { detectCapabilities } from "./capabilities";

describe("detectCapabilities", () => {
  beforeEach(() => {
    // jsdom's window.location isn't directly reassignable; strip the query instead.
    window.history.replaceState({}, "", "/");
    delete (window as unknown as { pywebview?: unknown }).pywebview;
  });

  it("reports the web shell (local:false) with no ?shell=desktop and no pywebview", async () => {
    const caps = await detectCapabilities();
    expect(caps).toEqual({ shell: "web", local: false, apps: false, adb: false, mic: true });
  });

  it("reports the desktop shell (local:true) once pywebview is present", async () => {
    window.history.replaceState({}, "", "/?shell=desktop");
    (window as unknown as { pywebview: unknown }).pywebview = {
      api: { capabilities: async () => ({ ok: true, adb: true, apps: true, mic: true }) },
    };

    const caps = await detectCapabilities();
    expect(caps.shell).toBe("desktop");
    expect(caps.local).toBe(true);
    expect(caps.adb).toBe(true);
    expect(caps.apps).toBe(true);
  });

  it("degrades honestly when ?shell=desktop is set but no bridge ever shows up", async () => {
    window.history.replaceState({}, "", "/?shell=desktop");
    const caps = await detectCapabilities(20); // short timeout -- just proving it degrades
    expect(caps.shell).toBe("desktop");
    expect(caps.local).toBe(true);
    expect(caps.adb).toBe(false);
    expect(caps.apps).toBe(false);
  });
});
