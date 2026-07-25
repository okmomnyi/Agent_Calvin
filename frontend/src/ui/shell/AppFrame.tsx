import type { ReactNode } from "react";
import { useAppStore } from "@/core/store";
import { ReactorRing } from "@/ui/hud/ReactorRing";
import { Waveform } from "@/ui/wave/Waveform";
import { Card } from "@/components/ui/card";
import { callBridge } from "@/core/desktopBridge";
import { cn } from "@/lib/utils";
import { StatusBar } from "./StatusBar";

const STATE_LABEL: Record<string, string> = {
  idle: "Idle",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  "awaiting-approval": "Needs your approval",
  error: "Something went wrong",
};

// The shared shell both the web dashboard and the desktop HUD render into, as cards in a
// two-column layout instead of one stacked column -- `chat` is the main column (reactor
// card on top, conversation filling the rest), `side` is the right-edge column (job
// listings, then whatever's pending approval). Below the `md` breakpoint (narrower windows,
// compact mode) it collapses to one column automatically.
export function AppFrame({
  shell,
  chat,
  side,
}: {
  shell: "web" | "desktop";
  chat?: ReactNode;
  side?: ReactNode;
}) {
  const hudState = useAppStore((s) => s.hudState);
  const micLevel = useAppStore((s) => s.micLevel);
  const micOn = useAppStore((s) => s.micOn);

  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <StatusBar shell={shell} />
      <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-hidden p-3 md:grid-cols-[1fr_300px]">
        <div className="flex min-h-0 min-w-0 flex-col gap-3">
          {/* Glass card: ring + waveform + mic control live together so "is it listening"
             is never split across two unrelated panels. bg-surface-hi/60 + backdrop-blur is
             the glass effect -- the window itself stays opaque (its own body background),
             this reads as frosted glass against the app's own dark UI, not the desktop. */}
          <Card className="shrink-0 overflow-hidden border-line/60 bg-surface-hi/60 backdrop-blur-xl">
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
              {shell === "desktop" && (
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
              )}
            </div>
          </Card>
          {chat}
        </div>
        {side && <div className="flex min-h-0 min-w-0 flex-col gap-3 overflow-hidden">{side}</div>}
      </main>
    </div>
  );
}
