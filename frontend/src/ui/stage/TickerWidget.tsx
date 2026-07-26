import { tokens } from "@/design/tokens";
import type { TickerWidgetData } from "@/core/stageTypes";

// The prototype's top strip, static (no jitter loop -- see agentos_stage_prototype.html's
// setInterval demo jitter, which is fake data and explicitly not ported). Every chip's
// change_pct came from a real fetch; there is nothing here to animate at rest.
export function TickerWidget({ data }: { data: TickerWidgetData }) {
  return (
    <div
      className="flex items-center gap-2 overflow-x-auto border-b border-line px-3 py-2 font-mono text-xs"
      role="list"
      aria-label="Market ticker"
    >
      <span className="shrink-0 font-mono text-[10px] tracking-widest text-text-mute">LIVE</span>
      {data.items.map((item) => {
        const color =
          item.change_pct > 0 ? tokens.color.good : item.change_pct < 0 ? tokens.color.bad : tokens.color.textDim;
        return (
          <span
            key={item.symbol}
            role="listitem"
            className="shrink-0 whitespace-nowrap rounded-[var(--radius-pill)] border border-line bg-surface px-3 py-1"
          >
            <b className="text-text">{item.symbol}</b>{" "}
            <span style={{ color }}>
              {item.change_pct > 0 ? "+" : ""}
              {item.change_pct.toFixed(1)}%
            </span>
          </span>
        );
      })}
    </div>
  );
}
