import { callBridge } from "@/core/desktopBridge";
import { api } from "@/core/transport";
import { useAppStore } from "@/core/store";
import { cn } from "@/lib/utils";

// Header strip: brand, live WS connection dot, a logout control once authenticated (S2),
// and -- desktop shell only -- a mic toggle and a minimize button. The window is frameless
// and always-on-top (client/hud_window.py) so it has no native title bar at all; without
// these two controls there is no way to start a voice turn or get the window out of the way.
export function StatusBar({ shell = "web" }: { shell?: "web" | "desktop" }) {
  const connected = useAppStore((s) => s.connected);
  const authState = useAppStore((s) => s.authState);
  const setAuthState = useAppStore((s) => s.setAuthState);
  const micOn = useAppStore((s) => s.micOn);

  const logout = () => {
    void api.logout().finally(() => setAuthState("anonymous"));
  };

  const toggleMic = () => {
    // client/hud_window.py's Bridge.toggle_mic() pushes the real new state back via
    // window.__agentosBridgeEvent (core/desktopBridge.ts) -- no optimistic update here,
    // the mic staying off until the OS stream genuinely opens is the whole point.
    void callBridge("toggle_mic");
  };

  const minimize = () => {
    void callBridge("hide");
  };

  return (
    <header className="flex h-11 shrink-0 items-center justify-between border-b border-line px-4">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "inline-block h-2 w-2 rounded-full transition-colors duration-150",
            connected ? "bg-primary" : "bg-text-mute",
          )}
          aria-label={connected ? "connected" : "disconnected"}
        />
        <span className="font-mono text-xs font-semibold tracking-[0.2em] text-text">
          AGENTOS
        </span>
      </div>
      <div className="flex items-center gap-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-text-dim">
          {shell} shell
        </span>
        {shell === "desktop" && (
          <button
            type="button"
            onClick={toggleMic}
            aria-pressed={micOn}
            aria-label={micOn ? "Turn microphone off" : "Turn microphone on"}
            className={cn(
              "rounded-[var(--radius-pill)] border px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest transition-colors duration-150",
              micOn
                ? "border-primary bg-primary/15 text-primary"
                : "border-line text-text-mute hover:text-text-dim",
            )}
          >
            {micOn ? "Mic On" : "Mic Off"}
          </button>
        )}
        {authState === "authenticated" && (
          <button
            type="button"
            onClick={logout}
            className="font-mono text-[10px] uppercase tracking-widest text-text-mute hover:text-text-dim"
          >
            Log out
          </button>
        )}
        {shell === "desktop" && (
          <button
            type="button"
            onClick={minimize}
            aria-label="Minimize"
            className="font-mono text-sm leading-none text-text-mute hover:text-text-dim"
          >
            —
          </button>
        )}
      </div>
    </header>
  );
}
