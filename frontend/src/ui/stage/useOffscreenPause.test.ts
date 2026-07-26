import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { createRef } from "react";
import { useOffscreenPause } from "./useOffscreenPause";

class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  callback: (entries: { isIntersecting: boolean }[]) => void;
  observed: Element[] = [];

  constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
    this.callback = callback;
    FakeIntersectionObserver.instances.push(this);
  }

  observe(el: Element) {
    this.observed.push(el);
  }

  disconnect() {
    this.observed = [];
  }

  fire(isIntersecting: boolean) {
    this.callback([{ isIntersecting }]);
  }
}

afterEach(() => {
  FakeIntersectionObserver.instances = [];
  vi.unstubAllGlobals();
});

describe("useOffscreenPause", () => {
  it("starts visible=true (fails open) before any observation fires", () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    const ref = createRef<HTMLDivElement>();
    Object.defineProperty(ref, "current", { value: document.createElement("div"), writable: true });

    const { result } = renderHook(() => useOffscreenPause(ref));
    expect(result.current).toBe(true);
  });

  it("flips to false when the element scrolls offscreen", () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    const el = document.createElement("div");
    const ref = { current: el };

    const { result } = renderHook(() => useOffscreenPause(ref));
    expect(FakeIntersectionObserver.instances).toHaveLength(1);

    act(() => FakeIntersectionObserver.instances[0].fire(false));
    expect(result.current).toBe(false);

    act(() => FakeIntersectionObserver.instances[0].fire(true));
    expect(result.current).toBe(true);
  });

  it("fails open (visible=true) when IntersectionObserver doesn't exist in this environment", () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    const ref = { current: document.createElement("div") };

    const { result } = renderHook(() => useOffscreenPause(ref));
    expect(result.current).toBe(true);
  });

  it("disconnects the observer on unmount", () => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
    const ref = { current: document.createElement("div") };

    const { unmount } = renderHook(() => useOffscreenPause(ref));
    const observer = FakeIntersectionObserver.instances[0];
    unmount();
    expect(observer.observed).toHaveLength(0);
  });
});
