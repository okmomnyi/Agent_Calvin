// Canvas voice waveform (A0). Driven by real mic level via `bus.emit("wave:level", 0..1)` —
// nothing in Slice 1 publishes that yet (no mic capture is wired into either shell), so it
// idles honestly rather than faking activity. Once wired (voice work in a later slice), the
// bars respond immediately; nothing here needs to change.
import { bus } from "../core/bus.js";

let canvas, ctx;
let target = 0;
let level = 0;
let active = false;
let colorActive = "#4fd8e8";
let colorIdle = "#2c3a40";
let raf = null;
let reduceMotion = false;

const BARS = 32;
const phases = Array.from({ length: BARS }, () => Math.random() * Math.PI * 2);

function resolveColors() {
  const style = getComputedStyle(document.documentElement);
  colorActive = style.getPropertyValue("--accent-cyan").trim() || colorActive;
  colorIdle = style.getPropertyValue("--line").trim() || colorIdle;
}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, canvas.clientWidth * dpr);
  canvas.height = Math.max(1, canvas.clientHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

export function mount(el) {
  canvas = el;
  ctx = canvas.getContext("2d");
  reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  resolveColors();
  resize();
  window.addEventListener("resize", resize);
  bus.on("wave:level", (v) => {
    target = Math.max(0, Math.min(1, Number(v) || 0));
  });
  bus.on("hud:state", ({ state }) => {
    active = state === "listening" || state === "speaking";
    if (!active) target = 0;
  });
  loop();
}

function loop() {
  raf = requestAnimationFrame(loop);
  level += (target - level) * 0.15;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  ctx.clearRect(0, 0, w, h);
  const barW = w / BARS;
  const step = reduceMotion ? 0.02 : active ? 0.12 : 0.03;
  for (let i = 0; i < BARS; i++) {
    phases[i] += step + (active && !reduceMotion ? Math.random() * 0.05 : 0);
    const noise = active ? 0.35 + level * 0.65 : 0.08;
    const amp = (Math.sin(phases[i]) * 0.5 + 0.5) * noise;
    const barH = Math.max(2, amp * h);
    const x = i * barW + barW * 0.2;
    const y = (h - barH) / 2;
    ctx.fillStyle = active ? colorActive : colorIdle;
    ctx.fillRect(x, y, barW * 0.6, barH);
  }
}

export function stop() {
  if (raf) cancelAnimationFrame(raf);
}
