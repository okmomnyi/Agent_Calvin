import { useEffect, useRef } from "react";
import { tokens } from "@/design/tokens";
import type { HudState } from "@/core/types";

// One canvas, one requestAnimationFrame loop, state driven by props -- it must NOT re-render
// through React each frame (that's how the glow stays at 60fps). Extended from the reference
// spec with more arc layers, a tick-mark dial ring, a center reticle, and a CSS boot-in
// fade/scale on the wrapping div (deliberately NOT inside the rAF loop -- a one-shot mount
// transition belongs to the DOM, not the per-frame draw call).
export function ReactorRing({ state, level = 0 }: { state: HudState; level?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);

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
      const s = cvs.clientWidth;
      cvs.width = cvs.height = s * dpr;
    };
    resize();
    addEventListener("resize", resize);

    const accent = accentFor(state);
    const glow = glowFor(state);
    const speed = state === "thinking" ? 2.2 : state === "listening" ? 1.4 : 0.5;

    const drawTicks = (s: number, c: number, rot: number) => {
      const count = 48;
      const inner = 0.47;
      const outer = 0.5;
      ctx.strokeStyle = accent;
      ctx.lineWidth = 1 * dpr;
      for (let i = 0; i < count; i++) {
        // Every 4th tick is a "major" mark, drawn brighter/longer -- purely decorative dial
        // detail, cheap enough to redraw every frame at 48 segments.
        const major = i % 4 === 0;
        ctx.globalAlpha = major ? 0.55 : 0.22;
        const a = rot + (i / count) * Math.PI * 2;
        const r0 = (major ? inner - 0.02 : inner) * s;
        const r1 = outer * s;
        ctx.beginPath();
        ctx.moveTo(c + Math.cos(a) * r0, c + Math.sin(a) * r0);
        ctx.lineTo(c + Math.cos(a) * r1, c + Math.sin(a) * r1);
        ctx.stroke();
      }
    };

    const drawReticle = (s: number, c: number) => {
      ctx.globalAlpha = 0.16;
      ctx.strokeStyle = accent;
      ctx.lineWidth = 1 * dpr;
      const len = 0.09 * s;
      ctx.beginPath();
      ctx.moveTo(c - len, c);
      ctx.lineTo(c + len, c);
      ctx.moveTo(c, c - len);
      ctx.lineTo(c, c + len);
      ctx.stroke();
    };

    const draw = () => {
      const s = cvs.width;
      const c = s / 2;
      ctx.clearRect(0, 0, s, s);

      // soft radial glow
      const g = ctx.createRadialGradient(c, c, 0, c, c, c);
      g.addColorStop(0, glow);
      g.addColorStop(1, "transparent");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, s, s);

      const baseRot = reduced ? 0 : t * 0.0006 * speed;
      drawTicks(s, c, baseRot * 0.3);

      // layered arcs, counter-rotating
      const rings = [
        { r: 0.44, dash: [18, 10], w: 2, dir: 1, a: 0.9 },
        { r: 0.38, dash: [2, 6], w: 1, dir: -1, a: 0.35 },
        { r: 0.34, dash: [4, 8], w: 1, dir: -1, a: 0.5 },
        { r: 0.28, dash: [10, 4, 2, 4], w: 1.5, dir: 1, a: 0.6 },
        { r: 0.24, dash: [], w: 2, dir: 1, a: 0.8 },
      ];
      for (const ring of rings) {
        ctx.beginPath();
        ctx.strokeStyle = accent;
        ctx.globalAlpha = ring.a;
        ctx.lineWidth = ring.w * dpr;
        ctx.setLineDash(ring.dash.map((d) => d * dpr));
        const rot = reduced ? 0 : t * 0.0006 * speed * ring.dir;
        ctx.arc(c, c, ring.r * s, rot, rot + Math.PI * 1.9);
        ctx.stroke();
      }

      drawReticle(s, c);

      // inner pulse — amplitude from voice level when listening/speaking
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
      const pulse =
        state === "listening" || state === "speaking"
          ? 0.12 + level * 0.16
          : 0.1 + (reduced ? 0 : Math.sin(t * 0.003) * 0.02);
      ctx.beginPath();
      ctx.fillStyle = accent;
      ctx.globalAlpha = 0.9;
      ctx.arc(c, c, pulse * s, 0, Math.PI * 2);
      ctx.fill();

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
    <div className="reactor-boot-in" style={{ width: "100%" }}>
      <canvas
        ref={ref}
        role="img"
        aria-label={`HUD state: ${state}`}
        style={{ width: "100%", aspectRatio: "1", display: "block" }}
      />
    </div>
  );
}

function accentFor(state: HudState): string {
  if (state === "awaiting-approval") return tokens.color.attention;
  if (state === "error") return tokens.color.bad;
  return tokens.color.primary;
}

function glowFor(state: HudState): string {
  if (state === "awaiting-approval") return tokens.glow.alert;
  if (state === "error") return hexToRgba(tokens.color.bad, 0.22);
  return tokens.glow.idle;
}

function hexToRgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}
