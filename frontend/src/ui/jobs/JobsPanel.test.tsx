import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { JobsPanel } from "./JobsPanel";
import { useAppStore } from "@/core/store";

describe("JobsPanel", () => {
  beforeEach(() => {
    useAppStore.setState({ jobs: [] });
  });

  it("shows an empty-state hint with no jobs", () => {
    render(<JobsPanel />);
    expect(screen.getByText(/No drafted or notified jobs/)).toBeInTheDocument();
  });

  it("renders each job's title, company, category, and score", () => {
    useAppStore.setState({
      jobs: [
        {
          id: 1, title: "Senior DevOps Engineer", company: "Acme", score: 85,
          category: "cloud_devops", status: "notified", apply_kind: "portal", apply_target: "https://x",
        },
      ],
    });
    render(<JobsPanel />);
    expect(screen.getByText("Senior DevOps Engineer")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("cloud_devops")).toBeInTheDocument();
    expect(screen.getByText("85")).toBeInTheDocument();
  });

  it("shows a dash instead of fabricating a score for an unscored job", () => {
    useAppStore.setState({
      jobs: [{ id: 2, title: "x", company: "y", score: null, category: null, status: "drafted", apply_kind: null, apply_target: null }],
    });
    render(<JobsPanel />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("shows the total count in the header", () => {
    useAppStore.setState({
      jobs: [
        { id: 1, title: "a", company: "x", score: 90, category: null, status: "drafted", apply_kind: null, apply_target: null },
        { id: 2, title: "b", company: "y", score: 40, category: null, status: "drafted", apply_kind: null, apply_target: null },
      ],
    });
    render(<JobsPanel />);
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("wraps a long title instead of overflowing the card", () => {
    const long = "Senior Software Engineer Cloud Architecture and Distributed Systems Platform Team Lead";
    useAppStore.setState({
      jobs: [{ id: 3, title: long, company: "x", score: 60, category: null, status: "drafted", apply_kind: null, apply_target: null }],
    });
    render(<JobsPanel />);
    expect(screen.getByText(long)).toHaveClass("break-words");
  });
});
