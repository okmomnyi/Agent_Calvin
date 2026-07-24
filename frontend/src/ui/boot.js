// Entry point + boot sequence (A2). Subsystem checks stream from the real /api/health
// response — a failed check shows red and stays visible, nothing here fabricates a pass.
// Total boot budget is 2.5s and any keypress skips straight to idle.
import { bus } from "../core/bus.js";
import * as capabilities from "../core/capabilities.js";
import { getToken, setToken, setSession, setCapabilities } from "../core/session.js";
import { health, fetchSession, voiceSocket, setServerBase } from "../core/transport.js";
import * as hud from "./hud.js";
import * as wave from "./wave.js";
import * as transcript from "./transcript.js";

// client/hud_window.py's local loopback static server (see its module docstring for why:
// Chromium's WebView2 blocks relative ES module imports under file://) puts the page under
// http://127.0.0.1:<ephemeral>/ — nothing like the droplet's own host:port. `?server=` on
// that URL carries the REAL kernel address so transport.js's API/WS calls go to the droplet
// instead of the static-file server that merely served this HTML. Read before anything else
// runs, since transport calls happen immediately in the boot sequence below.
const bootParams = new URLSearchParams(location.search);
const serverOverride = bootParams.get("server");
if (serverOverride) setServerBase(serverOverride);
import * as plansPanel from "./panels/plans.js";
import * as approvalsPanel from "./panels/approvals.js";
import * as jobsPanel from "./panels/jobs.js";
import * as briefingPanel from "./panels/briefing.js";
import * as musicPanel from "./panels/music.js";
import * as studyPanel from "./panels/study.js";
import * as weatherPanel from "./panels/weather.js";
import * as phonePanel from "./panels/phone.js";
import { bridge } from "../../shells/desktop/bridge.js";

const MAX_BOOT_MS = 2500;
const SETTLE_MS = 350;
const SESSION_POLL_MS = 15000;

const CHECKS = [
  { key: "db_ok", label: "database" },
  { key: "scheduler_running", label: "scheduler" },
  { key: "nim_key_present", label: "LLM route" },
  { key: "gmail_token", label: "gmail token", ok: (v) => !!v?.present },
  { key: "telegram_configured", label: "telegram" },
];

function timeout(ms) {
  return new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function bootLine(container, text, ok) {
  const row = document.createElement("div");
  row.className = "boot-line" + (ok === undefined ? "" : ok ? " ok" : " fail");
  row.textContent = (ok === undefined ? "… " : ok ? "✓ " : "✗ ") + text;
  container.appendChild(row);
}

async function runBootChecks() {
  const lines = document.getElementById("boot-lines");
  bootLine(lines, "contacting kernel");
  let h = null;
  try {
    h = await Promise.race([health(), timeout(MAX_BOOT_MS)]);
  } catch {
    /* falls through to the offline-degraded line below */
  }

  if (!h) {
    bootLine(lines, "kernel unreachable — continuing offline-degraded", false);
    return;
  }
  bootLine(lines, `kernel ${h.status === "ok" ? "online" : "degraded"}`, h.status === "ok");
  for (const check of CHECKS) {
    if (!(check.key in h)) continue;
    const raw = h[check.key];
    bootLine(lines, check.label, check.ok ? check.ok(raw) : !!raw);
  }
  if (h.queue && typeof h.queue === "object") {
    const depth = Object.values(h.queue).reduce((a, b) => a + (Number(b) || 0), 0);
    bootLine(lines, `queue depth ${depth}`, true);
  }
  await sleep(SETTLE_MS);
}

function waitForSkip() {
  return new Promise((resolve) => {
    const done = () => {
      window.removeEventListener("keydown", done);
      resolve();
    };
    window.addEventListener("keydown", done, { once: true });
  });
}

// client/hud_window.py's AssistantCore states (Phase 24: off/listening/recording/thinking/
// speaking) don't map 1:1 onto the HUD's states (A3: idle/listening/thinking/speaking/
// awaiting_approval/error) — RECORDING is still audibly "listening" from the HUD's point of
// view, and OFF is idle, not an error.
const BRIDGE_STATE_MAP = {
  off: "idle",
  listening: "listening",
  recording: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

function wireBridgeEvents(caps) {
  if (!caps.local) return; // web shell never receives these; nothing to wire
  bus.on("bridge:core", (payload) => {
    if (!payload) return;
    hud.setState(BRIDGE_STATE_MAP[payload.state] || "idle");
    transcript.syncFromBridge(payload.turns);
    // Compact mode expands on activity (A4) — collapses back is left to the hotkey/tray,
    // not an idle timeout, so a quiet HUD never surprises you by shrinking mid-read.
    if (payload.state && payload.state !== "off") {
      bridge.setCompact(false);
    }
  });
}

function applyCapabilities(caps) {
  document.querySelectorAll("[data-requires]").forEach((el) => {
    const needed = el.dataset.requires.split(",").map((s) => s.trim());
    el.hidden = !needed.every((key) => caps[key]);
  });
}

function wireToken(onChange) {
  const input = document.getElementById("token-input");
  const btn = document.getElementById("token-save");
  input.value = getToken();
  btn.addEventListener("click", () => {
    setToken(input.value.trim());
    onChange();
  });
}

async function refreshSession(ledEl) {
  if (!getToken()) {
    ledEl.className = "brand-dot";
    return;
  }
  try {
    const s = await fetchSession();
    setSession(s);
    ledEl.className = "brand-dot on";
  } catch {
    ledEl.className = "brand-dot off";
  }
}

async function main() {
  await Promise.race([runBootChecks(), waitForSkip()]);

  document.getElementById("boot").hidden = true;
  const app = document.getElementById("app");
  app.hidden = false;

  const caps = await capabilities.detect();
  setCapabilities(caps);
  document.getElementById("shell-meta").textContent = `${caps.shell} shell`;
  applyCapabilities(caps);

  hud.mount(document.getElementById("hud"));
  wave.mount(document.getElementById("wave"));
  transcript.mount({
    list: document.getElementById("transcript-list"),
    input: document.getElementById("transcript-input"),
    send: document.getElementById("transcript-send"),
  });

  approvalsPanel.mount(document.getElementById("panel-approvals"));
  plansPanel.mount(document.getElementById("panel-plans"));
  jobsPanel.mount(document.getElementById("panel-jobs"));
  briefingPanel.mount(document.getElementById("panel-briefing"));
  musicPanel.mount(document.getElementById("panel-music"));
  studyPanel.mount(document.getElementById("panel-study"));
  weatherPanel.mount(document.getElementById("panel-weather"));
  phonePanel.mount(document.getElementById("panel-phone"));

  wireBridgeEvents(caps);

  const skillLine = document.getElementById("hud-skill");
  bus.on("session:update", (s) => {
    approvalsPanel.render(s);
    plansPanel.render(s);
    jobsPanel.render(s);
    studyPanel.render(s);
    if (skillLine) skillLine.textContent = s?.live_skill_session ? `live: ${s.live_skill_session}` : "";
  });

  const led = document.getElementById("conn-led");
  wireToken(() => {
    refreshSession(led);
    voiceSocket.disconnect();
    voiceSocket.connect();
  });
  refreshSession(led);
  setInterval(() => refreshSession(led), SESSION_POLL_MS);

  voiceSocket.connect();
}

main();
