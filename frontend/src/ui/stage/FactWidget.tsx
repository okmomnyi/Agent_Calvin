import { Card } from "@/components/ui/card";
import type { FactWidgetData } from "@/core/stageTypes";
import type { StageAccent } from "@/core/stageTypes";

export function FactWidget({ data, accent = "primary" }: { data: FactWidgetData; accent?: StageAccent }) {
  return (
    <Card className="p-4">
      <h4 className="font-mono text-[11px] uppercase tracking-widest text-text-dim">{data.title}</h4>
      <div
        className={
          "mt-1.5 font-mono text-3xl " + (accent === "alert" ? "text-attention" : "text-primary")
        }
      >
        {data.stat}
      </div>
      <p className="mt-1 text-sm leading-snug text-text-dim">{data.sub}</p>
      {data.sources.length > 0 && (
        <p className="mt-2 font-mono text-[10px] tracking-wide text-text-mute">
          {data.sources.join(" · ")}
        </p>
      )}
    </Card>
  );
}
