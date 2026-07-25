import { useAppStore } from "@/core/store";
import { cn } from "@/lib/utils";

// Header strip: brand, shell/connection status, and (from Slice 0c/0d, wired in later
// slices) the login controls. Static/no-op here in S1 -- S2 wires it to real /api/session
// polling and S3 to the desktop shell's own status.
export function StatusBar({ shell = "web" }: { shell?: "web" | "desktop" }) {
  const connected = useAppStore((s) => s.connected);

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
      <div className="font-mono text-[10px] uppercase tracking-widest text-text-dim">
        {shell} shell
      </div>
    </header>
  );
}
