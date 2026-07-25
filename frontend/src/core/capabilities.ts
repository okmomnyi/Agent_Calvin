// Capability negotiation. Panels render conditionally on what THIS shell can actually do --
// local-only capabilities never appear-then-fail in the web shell.
//
// Deliberately NOT the simplified `apps: true, adb: true` shortcut for any desktop shell:
// the real pywebview bridge (client/hud_window.py's Bridge.capabilities()) still reports
// `adb: False, apps: False` until Slice 4 wires the server-side approval flow for them, and
// claiming otherwise here would be exactly the "fabricate a check that passed" §0 forbids --
// a control that renders and then does nothing. This asks the actual bridge and believes it.
export interface Capabilities {
  shell: "web" | "desktop";
  local: boolean;
  apps: boolean;
  adb: boolean;
  mic: boolean;
}

const WEB_CAPS: Capabilities = { shell: "web", local: false, apps: false, adb: false, mic: true };

function hasPywebview(): boolean {
  return typeof window !== "undefined" && !!(window as unknown as { pywebview?: unknown }).pywebview;
}

function waitForPywebviewReady(timeoutMs = 1500): Promise<boolean> {
  if (hasPywebview()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      window.removeEventListener("pywebviewready", onReady);
      resolve(ok);
    };
    const onReady = () => finish(hasPywebview());
    window.addEventListener("pywebviewready", onReady, { once: true });
    setTimeout(() => finish(hasPywebview()), timeoutMs);
  });
}

export async function detectCapabilities(bridgeTimeoutMs = 1500): Promise<Capabilities> {
  const params = new URLSearchParams(location.search);
  if (params.get("shell") !== "desktop") return WEB_CAPS;

  const base: Capabilities = { shell: "desktop", local: true, apps: false, adb: false, mic: true };
  await waitForPywebviewReady(bridgeTimeoutMs);
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  if (!api || typeof api.capabilities !== "function") {
    // Loaded with ?shell=desktop but the bridge never showed up -- degrade honestly rather
    // than claim capabilities that aren't actually wired up.
    return base;
  }
  try {
    const reported = (await (api.capabilities as () => Promise<Record<string, unknown>>)()) ?? {};
    const { ok: _ok, ...rest } = reported as { ok?: boolean } & Partial<Capabilities>;
    return { ...base, ...rest };
  } catch {
    return base;
  }
}
