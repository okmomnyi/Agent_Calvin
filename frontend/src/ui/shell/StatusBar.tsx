import { api } from "@/core/transport";
import { useAppStore } from "@/core/store";
import { cn } from "@/lib/utils";

// Header strip: brand, live WS connection dot, and a logout control once authenticated
// (S2). `connected` reflects the voice socket's actual state (core/ws.ts's VoiceSocket),
// not a static placeholder.
export function StatusBar({ shell = "web" }: { shell?: "web" | "desktop" }) {
  const connected = useAppStore((s) => s.connected);
  const authState = useAppStore((s) => s.authState);
  const setAuthState = useAppStore((s) => s.setAuthState);

  const logout = () => {
    void api.logout().finally(() => setAuthState("anonymous"));
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
        {authState === "authenticated" && (
          <button
            type="button"
            onClick={logout}
            className="font-mono text-[10px] uppercase tracking-widest text-text-mute hover:text-text-dim"
          >
            Log out
          </button>
        )}
      </div>
    </header>
  );
}
