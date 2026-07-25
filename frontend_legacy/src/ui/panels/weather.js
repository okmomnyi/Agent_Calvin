// Weather (Phase 36 Slice 3). skills/weather.py returns a structured `data.weather` dict
// alongside the one-sentence voice line — this renders that SAME dict structurally, so the
// panel and whatever was spoken can never disagree about what was actually fetched.
import { sendCommand } from "../transcript.js";

let el, outputEl;

export function mount(container) {
  el = container;
  el.replaceChildren();
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = "Get weather";
  btn.addEventListener("click", run);
  outputEl = document.createElement("div");
  outputEl.className = "panel-output dim";
  outputEl.textContent = "Ask for the weather to see it here.";
  el.append(btn, outputEl);
}

async function run() {
  outputEl.classList.remove("dim");
  outputEl.textContent = "…";
  try {
    const reply = await sendCommand("what's the weather");
    const report = reply.data && reply.data.weather;
    if (report) renderReport(report);
    else outputEl.textContent = reply.text; // honest failure message, not fabricated
  } catch (e) {
    outputEl.textContent = "⚠ " + e.message;
  }
}

function renderReport(report) {
  outputEl.replaceChildren();
  const city = document.createElement("div");
  city.className = "weather-city";
  city.textContent = report.city || "—";
  const cond = document.createElement("div");
  cond.className = "dim";
  cond.textContent = report.condition || "";
  const temps = document.createElement("div");
  temps.className = "mono";
  const bits = [];
  if (report.temperature != null) bits.push(`${Math.round(report.temperature)}°C now`);
  if (report.high != null && report.low != null) {
    bits.push(`H ${Math.round(report.high)}° / L ${Math.round(report.low)}°`);
  }
  temps.textContent = bits.join("   ");
  outputEl.append(city, cond, temps);
}

// No session-driven state for this panel — kept for interface symmetry with the others.
export function render() {}
