import { useEffect, useRef } from "react";
import { tokens } from "@/design/tokens";
import type { HudState } from "@/core/types";

const BAR_COUNT = 40;

// Same architecture as ReactorRing: one canvas, one rAF loop, state/level as props, no
// per-frame React re-render. `level` (0..1) is the real mic amplitude while listening/
// speaking; at rest it breathes gently rather than sitting dead flat, which is what makes
// "idle" read as alive instead of frozen.
export function Waveform({ state, level = 0 }: { state: HudState; level?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const history = useRef<number[]>(new Array(BAR_COUNT).fill(0.04));

  useEffect(() => {
    const cvs = ref.current;
    if (!cvs) return;
    const ctx = cvs.getContext("2d");
    if (!ctx) return;

    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let t = 0;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const resize = () => {
      cvs.width = cvs.clientWidth * dpr;
      cvs.height = cvs.clientHeight * dpr;
    };
    resize();
    addEventListener("resize", resize);

    const accent =
      state === "error"
        ? tokens.color.bad
        : state === "awaiting-approval"
          ? tokens.color.attention
          : tokens.color.primary;

    const draw = () => {
      const w = cvs.width;
      const h = cvs.height;
      ctx.clearRect(0, 0, w, h);

      const active = state === "listening" || state === "speaking";
      const target = active ? Math.max(0.06, level) : 0.05 + Math.sin(t * 0.002) * 0.02;
      const hist = history.current;
      hist.push(target * (0.7 + Math.random() * 0.3));
      hist.shift();

      const barW = w / BAR_COUNT;
      ctx.fillStyle = accent;
      for (let i = 0; i < BAR_COUNT; i++) {
        const amp = Math.max(0.03, hist[i]);
        const barH = amp * h;
        // Center-out fade so the ends of the strip taper rather than cutting off hard.
        const edgeFade = 1 - Math.abs(i - BAR_COUNT / 2) / (BAR_COUNT / 2);
        ctx.globalAlpha = 0.35 + edgeFade * 0.55;
        ctx.fillRect(i * barW + barW * 0.2, (h - barH) / 2, barW * 0.6, barH);
      }

      if (!reduced) {
        t += 16;
        raf = requestAnimationFrame(draw);
      }
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      removeEventListener("resize", resize);
    };
  }, [state, level]);

  return (
    <canvas
      ref={ref}
      role="img"
      aria-label={`Voice level, HUD state: ${state}`}
      style={{ width: "100%", height: "48px", display: "block" }}
    />
  );
}
