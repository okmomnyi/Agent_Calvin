import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ApprovalsList } from "./ApprovalsList";
import { useAppStore } from "@/core/store";

describe("ApprovalsList", () => {
  beforeEach(() => {
    useAppStore.setState({ pendingApprovals: [] });
  });

  it("renders nothing when there are no pending approvals", () => {
    const { container } = render(<ApprovalsList />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders each row's kind and what, matching core/session.py's shape", () => {
    useAppStore.setState({
      pendingApprovals: [
        { kind: "job", id: 7, what: "Backend Engineer @ Acme", action: "apply/skip" },
        { kind: "flashcard", id: 3, what: "What is a closure?", action: "approve/reject" },
      ],
    });
    render(<ApprovalsList />);
    expect(screen.getByText("Backend Engineer @ Acme")).toBeInTheDocument();
    expect(screen.getByText("Job")).toBeInTheDocument();
    expect(screen.getByText("What is a closure?")).toBeInTheDocument();
    expect(screen.getByText("Flashcard")).toBeInTheDocument();
  });

  it("shows the Telegram command for a kind that has one", () => {
    useAppStore.setState({
      pendingApprovals: [{ kind: "job", id: 7, what: "x", action: "apply/skip" }],
    });
    render(<ApprovalsList />);
    expect(screen.getByText("/jobs")).toBeInTheDocument();
  });

  it("never fabricates a Telegram command for a kind that doesn't have one (flip)", () => {
    useAppStore.setState({
      pendingApprovals: [{ kind: "flip", id: 9, what: "Some listing", action: "confirm" }],
    });
    render(<ApprovalsList />);
    expect(screen.queryByText(/^\//)).not.toBeInTheDocument();
  });

  it("has no action buttons -- this panel is read-only", () => {
    useAppStore.setState({
      pendingApprovals: [{ kind: "job", id: 7, what: "x", action: "apply/skip" }],
    });
    render(<ApprovalsList />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("truncates a long title instead of overflowing the row (min-w-0 + truncate on a flex item)", () => {
    const long = "Senior Software Engineer, Cloud Architecture & Distributed Systems Platform Team";
    useAppStore.setState({
      pendingApprovals: [{ kind: "job", id: 7, what: long, action: "apply/skip" }],
    });
    render(<ApprovalsList />);
    const el = screen.getByText(long);
    expect(el).toHaveClass("truncate");
    expect(el).toHaveClass("min-w-0");
  });
});
