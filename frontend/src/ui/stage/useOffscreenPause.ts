import { useEffect, useState, type RefObject } from "react";

// "pause offscreen loops" (Phase 38 build order step 7). Backed by IntersectionObserver so
// a widget scrolled out of the viewport (the stage can scroll on narrow/short windows,
// same as the prototype's own @media collapse to a single column) never keeps animating
// where nothing can see it. Environments with no IntersectionObserver at all (an old
// webview, or a test that hasn't stubbed it) fail OPEN -- visible=true -- since "assume
// it's on screen" is the safe default for a purely cosmetic pause, never the reverse.
export function useOffscreenPause<T extends Element>(ref: RefObject<T | null>): boolean {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;

    const observer = new IntersectionObserver(([entry]) => {
      setVisible(entry.isIntersecting);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);

  return visible;
}
