import type { ReactNode } from "react";
import { useAppStore } from "@/core/store";
import { ReactorRing } from "@/ui/hud/ReactorRing";
import { Waveform } from "@/ui/wave/Waveform";
import { StatusBar } from "./StatusBar";

const STATE_LABEL: Record<string, string> = {
  idle: "Idle",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
  "awaiting-approval": "Needs your approval",
  error: "Something went wrong",
};

// The shared shell both the web dashboard and the desktop HUD render into. Panels (S2)
// slot in as `children`; S1 is the frame + the reactor ring + status bar with no data yet.
export function AppFrame({ shell, children }: { shell: "web" | "desktop"; children?: ReactNode }) {
  const hudState = useAppStore((s) => s.hudState);
  const micLevel = useAppStore((s) => s.micLevel);

  return (
    <div className="flex h-full flex-col bg-bg text-text">
      <StatusBar shell={shell} />
      <main className="flex flex-1 flex-col items-center justify-center gap-4 overflow-hidden px-6">
        <div className="w-full max-w-[280px]">
          <ReactorRing state={hudState} level={micLevel} />
        </div>
        <p className="font-mono text-xs uppercase tracking-widest text-text-dim">
          {STATE_LABEL[hudState] ?? hudState}
        </p>
        <div className="w-full max-w-sm">
          <Waveform state={hudState} level={micLevel} />
        </div>
      </main>
      {children}
    </div>
  );
}
