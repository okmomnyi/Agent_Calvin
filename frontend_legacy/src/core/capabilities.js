// Capability negotiation (A0). Panels render conditionally on what THIS shell can
// actually do — local-only capabilities never appear-then-fail in the web shell.
//
// client/hud_window.py's window URL carries `?shell=desktop` (see boot.js) so this module
// knows to wait for pywebview's `pywebviewready` event before giving up — the bridge is
// injected asynchronously and, unlike the web shell, we *know* one is coming.
import { bridge } from "../../shells/desktop/bridge.js";

function isDesktopShell() {
  return new URLSearchParams(location.search).get("shell") === "desktop";
}

function waitForBridge(timeoutMs = 1500) {
  if (bridge.available()) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      window.removeEventListener("pywebviewready", onReady);
      resolve(ok);
    };
    const onReady = () => finish(bridge.available());
    window.addEventListener("pywebviewready", onReady, { once: true });
    setTimeout(() => finish(bridge.available()), timeoutMs);
  });
}

export async function detect() {
  if (!isDesktopShell()) {
    return { shell: "web", local: false, adb: false, apps: false, mic: true };
  }

  const base = { shell: "desktop", local: true, adb: false, apps: false, mic: true };
  await waitForBridge();
  if (!bridge.available()) {
    // Loaded with ?shell=desktop but the bridge never showed up — degrade honestly rather
    // than claim capabilities that aren't actually wired up.
    return base;
  }
  const reported = await bridge.capabilities();
  if (!reported || reported.ok === false) return base;
  const { ok: _ok, ...capsOnly } = reported; // `ok` is transport plumbing, not a capability
  return { ...base, ...capsOnly };
}
