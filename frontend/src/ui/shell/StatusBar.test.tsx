import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatusBar } from "./StatusBar";
import { useAppStore } from "@/core/store";

vi.mock("@/core/desktopBridge", () => ({ callBridge: vi.fn() }));

describe("StatusBar", () => {
  beforeEach(() => {
    useAppStore.setState({ connected: false, authState: "unknown", micOn: false });
  });

  it("shows no mic, minimize, or close control on the web shell", () => {
    render(<StatusBar shell="web" />);
    expect(screen.queryByLabelText(/microphone/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Minimize")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Close")).not.toBeInTheDocument();
  });

  it("shows a mic toggle, minimize, and close on the desktop shell", () => {
    render(<StatusBar shell="desktop" />);
    expect(screen.getByLabelText("Turn microphone on")).toBeInTheDocument();
    expect(screen.getByLabelText("Minimize")).toBeInTheDocument();
    expect(screen.getByLabelText("Close")).toBeInTheDocument();
  });

  it("reflects micOn in the button's label and pressed state", () => {
    useAppStore.setState({ micOn: true });
    render(<StatusBar shell="desktop" />);
    const btn = screen.getByLabelText("Turn microphone off");
    expect(btn).toHaveAttribute("aria-pressed", "true");
  });

  it("clicking the mic button calls the bridge's toggle_mic, with no optimistic state change", async () => {
    const { callBridge } = await import("@/core/desktopBridge");
    render(<StatusBar shell="desktop" />);
    await userEvent.click(screen.getByLabelText("Turn microphone on"));
    expect(callBridge).toHaveBeenCalledWith("toggle_mic");
    expect(useAppStore.getState().micOn).toBe(false); // unchanged until the bridge event arrives
  });

  it("clicking minimize calls the bridge's hide", async () => {
    const { callBridge } = await import("@/core/desktopBridge");
    render(<StatusBar shell="desktop" />);
    await userEvent.click(screen.getByLabelText("Minimize"));
    expect(callBridge).toHaveBeenCalledWith("hide");
  });

  it("clicking close calls the bridge's quit", async () => {
    const { callBridge } = await import("@/core/desktopBridge");
    render(<StatusBar shell="desktop" />);
    await userEvent.click(screen.getByLabelText("Close"));
    expect(callBridge).toHaveBeenCalledWith("quit");
  });
});
