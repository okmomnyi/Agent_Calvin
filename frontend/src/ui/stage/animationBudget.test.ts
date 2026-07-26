import { afterEach, describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { MAX_ANIMATING_WIDGETS, stageAnimationBudget, useAnimationSlot } from "./animationBudget";

afterEach(() => {
  stageAnimationBudget.reset();
});

describe("stageAnimationBudget", () => {
  it("grants up to MAX_ANIMATING_WIDGETS slots and refuses beyond that", () => {
    const tokens = Array.from({ length: MAX_ANIMATING_WIDGETS }, () =>
      stageAnimationBudget.requestSlot(),
    );
    expect(tokens.every((t) => t !== null)).toBe(true);
    expect(stageAnimationBudget.requestSlot()).toBeNull();
  });

  it("frees a slot on release so a later requester can take it", () => {
    const first = stageAnimationBudget.requestSlot();
    stageAnimationBudget.releaseSlot(first);
    expect(stageAnimationBudget.requestSlot()).not.toBeNull();
  });
});

describe("useAnimationSlot", () => {
  it("grants a slot when enabled and the budget has room", () => {
    const { result } = renderHook(() => useAnimationSlot(true));
    expect(result.current).toBe(true);
  });

  it("never exceeds the cap across concurrently mounted widgets", () => {
    const hooks = Array.from({ length: MAX_ANIMATING_WIDGETS + 3 }, () =>
      renderHook(() => useAnimationSlot(true)),
    );
    const grantedCount = hooks.filter((h) => h.result.current).length;
    expect(grantedCount).toBe(MAX_ANIMATING_WIDGETS);
  });

  it("releases its slot on unmount, freeing it for the next widget", () => {
    const first = renderHook(() => useAnimationSlot(true));
    expect(first.result.current).toBe(true);
    first.unmount();

    const second = renderHook(() => useAnimationSlot(true));
    expect(second.result.current).toBe(true);
  });

  it("releases its slot when disabled without unmounting", () => {
    const { result, rerender } = renderHook(({ enabled }) => useAnimationSlot(enabled), {
      initialProps: { enabled: true },
    });
    expect(result.current).toBe(true);

    rerender({ enabled: false });
    expect(result.current).toBe(false);

    const other = renderHook(() => useAnimationSlot(true));
    expect(other.result.current).toBe(true);
  });

  it("a denied widget renders statically -- false, not a retry loop", () => {
    for (let i = 0; i < MAX_ANIMATING_WIDGETS; i++) renderHook(() => useAnimationSlot(true));
    const denied = renderHook(() => useAnimationSlot(true));
    expect(denied.result.current).toBe(false);
  });
});
