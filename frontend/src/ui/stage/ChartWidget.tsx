import { useEffect, useRef } from "react";
import { Card } from "@/components/ui/card";
import { tokens } from "@/design/tokens";
import type { ChartWidgetData } from "@/core/stageTypes";

// Hand-rolled canvas sparkline -- same architecture as ReactorRing/Waveform (one canvas,
// no per-frame React re-render), and deliberately NOT a charting library: `series` is a
// fixed historical snapshot from ONE real fetch (skills/markets.py's display()), not a
// live-ticking feed, so there is nothing here to animate continuously -- one draw per
// dataset is honest, and it keeps this widget out of the animation budget entirely (see
// ui/stage/animationBudget.ts's docstring: "ring + <=2", not "ring + <=2 + every chart").
// The prototype's own chart (agentos_stage_prototype.html) is ALSO hand-drawn canvas with
// no library at all -- this keeps that visual language while replacing its fake
// random-walk data and its setInterval "live" jitter with one real, static series.
export function ChartWidget({ data }: { data: ChartWidgetData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const first = data.series[0]?.v;
  const last = data.series[data.series.length - 1]?.v;
  const dir = first !== undefined && last !== undefined ? last - first : 0;
  const changePct = first ? (dir / first) * 100 : 0;
  const color = dir >= 0 ? tokens.color.good : tokens.color.bad;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(devicePixelRatio || 1, 2);
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const values = data.series.map((p) => p.v);
    if (values.length < 2) {
      if (values.length === 1) {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(w / 2, h / 2, 3 * dpr, 0, Math.PI * 2);
        ctx.fill();
      }
      return;
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const x = (i: number) => (i / (values.length - 1)) * w;
    const y = (v: number) => h - 8 * dpr - ((v - min) / range) * (h - 16 * dpr);

    ctx.beginPath();
    ctx.moveTo(0, h);
    values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.lineTo(w, h);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, dir >= 0 ? "rgba(93,202,165,0.25)" : "rgba(226,86,74,0.25)");
    gradient.addColorStop(1, "transparent");
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    values.forEach((v, i) => (i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 2 * dpr;
    ctx.stroke();

    ctx.beginPath();
    ctx.fillStyle = color;
    ctx.arc(x(values.length - 1), y(values[values.length - 1]), 3 * dpr, 0, Math.PI * 2);
    ctx.fill();
  }, [data.series, color, dir]);

  const asOf = new Date(data.as_of * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <Card className="p-4">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="font-mono text-[11px] uppercase tracking-widest text-text-dim">
          {data.asset} · {data.klass}
        </h4>
        {last !== undefined && (
          <span className="font-mono text-base text-text">
            {last.toLocaleString(undefined, { maximumFractionDigits: 2 })}{" "}
            <span style={{ color }}>
              {dir >= 0 ? "+" : ""}
              {changePct.toFixed(1)}%
            </span>
          </span>
        )}
      </div>
      <canvas ref={canvasRef} className="mt-2 block h-[100px] w-full" role="img" aria-label={`${data.asset} price chart`} />
      <p className="mt-2 font-mono text-[10px] tracking-wide text-text-mute">
        AS OF {asOf} · {data.delayed_label} · {data.source}
      </p>
    </Card>
  );
}
