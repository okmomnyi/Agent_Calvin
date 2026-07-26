import { useEffect, useRef, type ReactNode } from "react";
import { motion, type TargetAndTransition } from "framer-motion";
import { ReactorRing } from "@/ui/hud/ReactorRing";
import { Waveform } from "@/ui/wave/Waveform";
import { callBridge } from "@/core/desktopBridge";
import { cn } from "@/lib/utils";
import { useReducedMotion } from "@/core/reducedMotion";
import { useAppStore } from "@/core/store";
import { widgetKey, type StageWidget, type StageTransition } from "@/core/stageTypes";
import type { HudState } from "@/core/types";
import { useAnimationSlot } from "./animationBudget";
import { useOffscreenPause } from "./useOffscreenPause";
import { ChartWidget } from "./ChartWidget";
import { ArticlesWidget } from "./ArticlesWidget";
import { FactWidget } from "./FactWidget";
import { TickerWidget } from "./TickerWidget";

const STATE_LABEL: Record<string, string> = {
  idle: "Idle",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  "awaiting-approval": "Needs your approval",
  error: "Something went wrong",
};

// Enter-only (no exit animation): matches the prototype's own choreography exactly --
// agentos_stage_prototype.html's clearSlots() hides the old scene INSTANTLY (a plain CSS
// class removal, no fade-out), then rebuilds and blooms in the new one after a short
// pause. Mirroring that here means a widget swap is one clean transition, not a fade-out
// racing a fade-in, and (practically) it makes "did the swap happen" a synchronous fact
// instead of something gated behind an animation's own completion callback.
const VARIANTS: Record<StageTransition, { initial: TargetAndTransition; animate: TargetAndTransition }> = {
  bloom: { initial: { opacity: 0, y: 10 }, animate: { opacity: 1, y: 0 } },
  swap: { initial: { opacity: 0 }, animate: { opacity: 1 } },
  settle: { initial: { opacity: 1, y: 0 }, animate: { opacity: 1, y: 0 } },
};

/** A widget frozen by pin() keeps its OWN content even while `widgets` (the latest
 * directive) changes underneath it -- present in every slot it applies to, and surviving a
 * directive that doesn't mention that widget type at all. */
function mergeWithPin(widgets: StageWidget[], pinned: StageWidget | null): StageWidget[] {
  if (!pinned) return widgets;
  return [pinned, ...widgets.filter((w) => w.type !== pinned.type)];
}

function assignSlots(widgets: StageWidget[]) {
  let ticker: Extract<StageWidget, { type: "ticker" }> | null = null;
  let left: Extract<StageWidget, { type: "fact" | "map" }> | null = null;
  let right: Extract<StageWidget, { type: "chart" }> | null = null;
  let bottom: Extract<StageWidget, { type: "articles" }> | null = null;
  for (const w of widgets) {
    if (w.type === "ticker" && !ticker) ticker = w;
    else if (w.type === "chart" && !right) right = w;
    else if ((w.type === "fact" || w.type === "map") && !left) left = w;
    else if (w.type === "articles" && !bottom) bottom = w;
  }
  return { ticker, left, right, bottom };
}

/** One choreographed slot: bloom/swap/settle in, reverse out, capped by the shared
 * animation budget and paused when scrolled offscreen; reduced-motion always collapses to
 * an instant appearance with no loop ever started. */
function Slot({
  slotKey,
  transition,
  className,
  children,
}: {
  slotKey: string;
  transition: StageTransition;
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const visible = useOffscreenPause(ref);
  const granted = useAnimationSlot(!reduced && visible);
  const animate = granted && !reduced;
  const variant = VARIANTS[transition];

  return (
    <motion.div
      key={slotKey}
      ref={ref}
      className={className}
      initial={animate ? variant.initial : false}
      animate={variant.animate}
      transition={animate ? { duration: transition === "bloom" ? 0.5 : 0.25 } : { duration: 0 }}
    >
      {children}
    </motion.div>
  );
}

function WidgetSlot({ widget, transition, className }: { widget: StageWidget; transition: StageTransition; className?: string }) {
  return (
    <Slot slotKey={widgetKey(widget)} transition={transition} className={className}>
      {widget.type === "chart" && <ChartWidget data={widget} />}
      {widget.type === "articles" && <ArticlesWidget data={widget} />}
      {widget.type === "fact" && <FactWidget data={widget} />}
      {widget.type === "ticker" && <TickerWidget data={widget} />}
      {widget.type === "map" && (
        <div className="rounded-[var(--radius-card)] border border-line bg-surface-hi p-4 text-xs text-text-dim">
          {widget.region}
        </div>
      )}
    </Slot>
  );
}

export function StageCanvas({
  hudState,
  micLevel,
  micOn,
  shell,
}: {
  hudState: HudState;
  micLevel: number;
  micOn: boolean;
  shell: "web" | "desktop";
}) {
  const directive = useAppStore((s) => s.stageDirective);
  const pinnedWidget = useAppStore((s) => s.pinnedWidget);
  const setStageDirective = useAppStore((s) => s.setStageDirective);
  const pinWidget = useAppStore((s) => s.pinWidget);
  const unpinWidget = useAppStore((s) => s.unpinWidget);

  // ttl_s idle-return: a client-side decay back to calm, entirely local -- no server
  // round-trip needed to "ask" the stage to rest again. Re-armed only when the DIRECTIVE
  // reference actually changes (one real WS event), never on an unrelated re-render.
  useEffect(() => {
    if (!directive?.ttl_s) return;
    const id = window.setTimeout(() => {
      setStageDirective({ focus: null, transition: "settle", widgets: [] });
    }, directive.ttl_s * 1000);
    return () => window.clearTimeout(id);
  }, [directive, setStageDirective]);

  const rawWidgets = directive?.widgets ?? [];
  const widgets = mergeWithPin(rawWidgets, pinnedWidget);
  const isIdle = widgets.length === 0 && !directive?.focus;
  const accent = directive?.accent ?? "primary";
  const transition = directive?.transition ?? "settle";
  const { ticker, left, right, bottom } = assignSlots(widgets);

  if (isIdle) {
    // Degrades EXACTLY to the Phase 36 idle HUD -- same markup as before this phase, so a
    // presenter/feed failure (or simply nothing to show yet) never looks different from
    // the pre-Phase-38 app.
    return (
      <div className="flex flex-col items-center gap-3 px-6 py-6">
        <div className="w-full max-w-[220px]">
          <ReactorRing state={hudState} level={micLevel} />
        </div>
        <p className="font-mono text-xs uppercase tracking-widest text-text-dim">
          {STATE_LABEL[hudState] ?? hudState}
        </p>
        <div className="w-full max-w-sm">
          <Waveform state={hudState} level={micLevel} />
        </div>
        {shell === "desktop" && <MicButton micOn={micOn} />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 px-4 py-4" data-testid="stage-active">
      {ticker && <WidgetSlot key={widgetKey(ticker)} widget={ticker} transition="swap" />}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_auto_1fr]">
        <div className="flex flex-col gap-3 md:order-1">
          {left && <WidgetSlot key={widgetKey(left)} widget={left} transition={transition} />}
        </div>

        <div className="flex flex-col items-center justify-center gap-2 md:order-2">
          <div className="w-full max-w-[160px]">
            <ReactorRing state={hudState} level={micLevel} accentOverride={accent} />
          </div>
          <div className="text-center">
            <p className="font-mono text-[10px] uppercase tracking-[0.3em] text-text-mute">focus</p>
            <p className="min-h-[1.25rem] text-sm text-text" data-testid="stage-focus-label">
              {directive?.focus ?? "standing by"}
            </p>
          </div>
          {directive?.headline && (
            <p className="max-w-[280px] text-center text-xs leading-relaxed text-text-dim">{directive.headline}</p>
          )}
          {shell === "desktop" && <MicButton micOn={micOn} />}
        </div>

        <div className="flex flex-col gap-3 md:order-3">
          {right && <WidgetSlot key={widgetKey(right)} widget={right} transition={transition} />}
        </div>
      </div>

      {bottom && <WidgetSlot key={widgetKey(bottom)} widget={bottom} transition={transition} />}

      <PinControls
        widgets={widgets}
        pinnedType={pinnedWidget?.type ?? null}
        onPin={pinWidget}
        onUnpin={unpinWidget}
      />
    </div>
  );
}

function MicButton({ micOn }: { micOn: boolean }) {
  return (
    <button
      type="button"
      onClick={() => void callBridge("toggle_mic")}
      aria-pressed={micOn}
      aria-label={micOn ? "Turn microphone off" : "Turn microphone on"}
      className={cn(
        "rounded-[var(--radius-pill)] border px-4 py-1.5 font-mono text-xs uppercase tracking-widest transition-colors duration-150",
        micOn
          ? "border-primary bg-primary/15 text-primary"
          : "border-line bg-surface text-text-dim hover:text-text",
      )}
    >
      {micOn ? "● Listening — tap to stop" : "Tap to talk"}
    </button>
  );
}

/** Manual pin control: one button per pinnable widget currently on stage. Mirrors the
 * voice path ("pin that" / "unpin") exactly -- both end at the same store actions. */
function PinControls({
  widgets,
  pinnedType,
  onPin,
  onUnpin,
}: {
  widgets: StageWidget[];
  pinnedType: StageWidget["type"] | null;
  onPin: (w: StageWidget) => void;
  onUnpin: () => void;
}) {
  const pinnable = widgets.filter((w): w is Extract<StageWidget, { type: "chart" | "fact" | "articles" }> =>
    w.type === "chart" || w.type === "fact" || w.type === "articles",
  );
  if (pinnable.length === 0) return null;
  return (
    <div className="flex flex-wrap justify-center gap-2 pt-1">
      {pinnable.map((w) => {
        const isPinned = pinnedType === w.type;
        return (
          <button
            key={widgetKey(w)}
            type="button"
            onClick={() => (isPinned ? onUnpin() : onPin(w))}
            aria-pressed={isPinned}
            className={cn(
              "rounded-[var(--radius-pill)] border px-3 py-1 font-mono text-[10px] uppercase tracking-widest transition-colors",
              isPinned
                ? "border-primary bg-primary/15 text-primary"
                : "border-line bg-surface text-text-dim hover:text-text",
            )}
          >
            {isPinned ? `Unpin ${w.type}` : `Pin ${w.type}`}
          </button>
        );
      })}
    </div>
  );
}
