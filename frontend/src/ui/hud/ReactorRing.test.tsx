import { describe, expect, it, vi, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { ReactorRing } from "./ReactorRing";

describe("ReactorRing", () => {
  afterEach(cleanup);

  it("cancels its rAF loop on unmount (no leaked animation loop)", () => {
    const raf = vi.spyOn(window, "requestAnimationFrame").mockReturnValue(1);
    const caf = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});

    const { unmount } = render(<ReactorRing state="idle" />);
    expect(raf).toHaveBeenCalled();

    unmount();
    expect(caf).toHaveBeenCalled();

    raf.mockRestore();
    caf.mockRestore();
  });

  it("with reduced motion, draws one static frame and schedules no animation loop", () => {
    const matchMediaMock = vi.fn().mockReturnValue({ matches: true } as MediaQueryList);
    vi.stubGlobal("matchMedia", matchMediaMock);
    const raf = vi.spyOn(window, "requestAnimationFrame");

    render(<ReactorRing state="idle" />);

    expect(matchMediaMock).toHaveBeenCalledWith("(prefers-reduced-motion: reduce)");
    expect(raf).not.toHaveBeenCalled();

    raf.mockRestore();
    vi.unstubAllGlobals();
  });

  it("renders an accessible label reflecting the current state", () => {
    const { getByRole } = render(<ReactorRing state="awaiting-approval" />);
    expect(getByRole("img", { name: /awaiting-approval/i })).toBeInTheDocument();
  });
});
