import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// jsdom has no real <canvas> renderer (no native `canvas` package here -- deliberately not
// added just to satisfy tests). ReactorRing/Waveform/the stage's ChartWidget only ever call
// a small, fixed set of CanvasRenderingContext2D methods, so a minimal stub is enough to
// let their effects run and be asserted on (rAF scheduled/cancelled, etc.) without pulling
// in native canvas rendering, which no CI environment here needs.
class FakeGradient {
  addColorStop() {}
}

function fakeContext() {
  return {
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    arc: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    setLineDash: vi.fn(),
    createRadialGradient: vi.fn(() => new FakeGradient()),
    createLinearGradient: vi.fn(() => new FakeGradient()),
    strokeStyle: "",
    fillStyle: "",
    lineWidth: 0,
    globalAlpha: 1,
  } as unknown as CanvasRenderingContext2D;
}

HTMLCanvasElement.prototype.getContext = vi.fn(() => fakeContext()) as unknown as typeof HTMLCanvasElement.prototype.getContext;

// jsdom does not implement matchMedia at all. Default to "not reduced motion" (matches:
// false) so the ordinary animation path runs in tests by default; individual tests override
// this with vi.stubGlobal("matchMedia", ...) when they specifically need the reduced-motion
// branch, same pattern as ReactorRing.test.tsx.
// jsdom doesn't implement scrollTo on elements either -- ChatPanel's autoscroll is the only
// caller today, but stubbing it at the Element level (like matchMedia above) means any future
// scrolling component gets it for free too.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = vi.fn();
}

if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}
