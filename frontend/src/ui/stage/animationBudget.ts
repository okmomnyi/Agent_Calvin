import { useEffect, useRef, useState } from "react";

// Performance budget (Phase 38 build order step 7): "cap concurrently animating widgets
// (ring + <=2)". The ReactorRing's own rAF canvas loop is separate and always allowed --
// this budget governs everything ELSE that wants to run an entrance/exit transition or a
// continuous loop at the same time. A widget denied a slot still renders; it just skips
// its OWN animation (see useAnimationSlot below) rather than being hidden or delayed.
export const MAX_ANIMATING_WIDGETS = 2;

class AnimationBudget {
  private active = new Set<symbol>();

  requestSlot(): symbol | null {
    if (this.active.size >= MAX_ANIMATING_WIDGETS) return null;
    const token = Symbol("stage-animation-slot");
    this.active.add(token);
    return token;
  }

  releaseSlot(token: symbol | null): void {
    if (token) this.active.delete(token);
  }

  get activeCount(): number {
    return this.active.size;
  }

  /** Test/dev only -- a stale suite run must never leak slots into the next test. */
  reset(): void {
    this.active.clear();
  }
}

export const stageAnimationBudget = new AnimationBudget();

/**
 * Requests one of the shared animation slots while `enabled` is true (visible, not
 * reduced-motion, has something to animate); releases it on unmount or when `enabled`
 * flips false. Returns whether THIS caller currently holds a slot -- a widget that is
 * denied one (the cap is already full) must render its content statically rather than
 * queue or retry, since Phase 38's motion model is calm-at-rest, not a waiting room.
 */
export function useAnimationSlot(enabled: boolean): boolean {
  const tokenRef = useRef<symbol | null>(null);
  const [granted, setGranted] = useState(false);

  useEffect(() => {
    if (!enabled) {
      stageAnimationBudget.releaseSlot(tokenRef.current);
      tokenRef.current = null;
      setGranted(false);
      return;
    }
    const token = stageAnimationBudget.requestSlot();
    tokenRef.current = token;
    setGranted(token !== null);
    return () => {
      stageAnimationBudget.releaseSlot(tokenRef.current);
      tokenRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-request only on enabled flip
  }, [enabled]);

  return granted;
}
