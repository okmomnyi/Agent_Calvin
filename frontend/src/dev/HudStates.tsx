import { useEffect, useState } from "react";
import { AppFrame } from "@/ui/shell/AppFrame";
import { useAppStore } from "@/core/store";
import { HUD_STATES } from "@/core/types";
import { Button } from "@/components/ui/button";

// S1's stop-gate: "show the HUD idle and cycling through states in Storybook or a dev
// route." No Storybook dependency added for one harness page -- this is that dev route,
// reached at ?dev=hud in a dev build (see src/App.tsx). Never bundled into the real app UI.
export function HudStatesDevRoute() {
  const hudState = useAppStore((s) => s.hudState);
  const setHudState = useAppStore((s) => s.setHudState);
  const micLevel = useAppStore((s) => s.micLevel);
  const setMicLevel = useAppStore((s) => s.setMicLevel);
  const [connected, setConnected] = useState(false);
  const setConnectedStore = useAppStore((s) => s.setConnected);
  const [autoCycle, setAutoCycle] = useState(true);

  useEffect(() => {
    setConnectedStore(connected);
  }, [connected, setConnectedStore]);

  useEffect(() => {
    if (!autoCycle) return;
    let i = HUD_STATES.indexOf(hudState);
    const id = setInterval(() => {
      i = (i + 1) % HUD_STATES.length;
      setHudState(HUD_STATES[i]);
    }, 1800);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally only re-arms on toggle
  }, [autoCycle]);

  useEffect(() => {
    if (hudState !== "listening" && hudState !== "speaking") return;
    const id = setInterval(() => setMicLevel(Math.random()), 120);
    return () => clearInterval(id);
  }, [hudState, setMicLevel]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1">
        <AppFrame shell="web" />
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-line bg-surface p-3">
        <span className="mr-2 font-mono text-[10px] uppercase tracking-widest text-text-mute">
          dev: hud states
        </span>
        {HUD_STATES.map((s) => (
          <Button
            key={s}
            size="sm"
            variant={s === hudState ? "default" : "outline"}
            onClick={() => {
              setAutoCycle(false);
              setHudState(s);
            }}
          >
            {s}
          </Button>
        ))}
        <Button size="sm" variant={autoCycle ? "default" : "ghost"} onClick={() => setAutoCycle((v) => !v)}>
          {autoCycle ? "auto-cycling" : "auto-cycle off"}
        </Button>
        <Button size="sm" variant={connected ? "default" : "ghost"} onClick={() => setConnected((v) => !v)}>
          {connected ? "connected" : "disconnected"}
        </Button>
        <span className="ml-2 font-mono text-[10px] text-text-mute">level {micLevel.toFixed(2)}</span>
      </div>
    </div>
  );
}
