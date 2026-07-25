import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import { useAppStore } from "@/core/store";

describe("ChatPanel", () => {
  beforeEach(() => {
    useAppStore.setState({ turns: [] });
  });

  it("shows an empty-state hint with no turns", () => {
    render(<ChatPanel onSend={vi.fn()} sending={false} />);
    expect(screen.getByText(/No turns yet/)).toBeInTheDocument();
  });

  it("renders each turn's sent text and reply", () => {
    useAppStore.setState({
      turns: [{ text: "what's my next job app due", reply: "Two are due today.", channel: "dashboard", at: 1, skill: "job_hunter" }],
    });
    render(<ChatPanel onSend={vi.fn()} sending={false} />);
    expect(screen.getByText("what's my next job app due")).toBeInTheDocument();
    expect(screen.getByText("Two are due today.")).toBeInTheDocument();
    expect(screen.getByText("job_hunter")).toBeInTheDocument();
  });

  it("calls onSend with the typed text and clears the input", async () => {
    const onSend = vi.fn();
    render(<ChatPanel onSend={onSend} sending={false} />);
    const input = screen.getByPlaceholderText("Type a command…");
    await userEvent.type(input, "status");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(onSend).toHaveBeenCalledWith("status");
    expect(input).toHaveValue("");
  });

  it("does not send an empty or whitespace-only draft", async () => {
    const onSend = vi.fn();
    render(<ChatPanel onSend={onSend} sending={false} />);
    await userEvent.type(screen.getByPlaceholderText("Type a command…"), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("disables sending while a request is in flight", () => {
    render(<ChatPanel onSend={vi.fn()} sending={true} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("wraps a bare URL instead of forcing the panel to scroll sideways", () => {
    const url = "https://himalayas.app/companies/peek/jobs/senior-dev-ops-engineer-with-a-very-long-slug";
    useAppStore.setState({
      turns: [{ text: "apply link", reply: url, channel: "dashboard", at: 1, skill: null }],
    });
    render(<ChatPanel onSend={vi.fn()} sending={false} />);
    expect(screen.getByText(url)).toHaveClass("break-words");
  });
});
