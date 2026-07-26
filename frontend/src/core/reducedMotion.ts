import { useEffect, useState } from "react";

// Shared with ReactorRing/Waveform's own inline `matchMedia` checks, but exposed as a hook
// so components that need to RE-RENDER on a live preference change (the stage's
// choreography, not a canvas rAF loop that just reads it once per effect run) can react
// to it -- a user toggling the OS setting mid-session must drop straight to instant
// transitions, not wait for a remount.
export function prefersReducedMotion(): boolean {
  return typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion);

  useEffect(() => {
    if (typeof matchMedia === "undefined") return;
    const mql = matchMedia("(prefers-reduced-motion: reduce)");
    const handler = () => setReduced(mql.matches);
    mql.addEventListener?.("change", handler);
    return () => mql.removeEventListener?.("change", handler);
  }, []);

  return reduced;
}
