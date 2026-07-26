import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { FactWidget } from "./FactWidget";
import type { FactWidgetData } from "@/core/stageTypes";

afterEach(cleanup);

const data: FactWidgetData = {
  type: "fact",
  title: "CORROBORATION",
  stat: "4",
  sub: "independent sources reporting this",
  sources: ["Reuters", "AP"],
};

describe("FactWidget", () => {
  it("renders the stat, sub, and sources", () => {
    render(<FactWidget data={data} />);
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText(/independent sources/)).toBeInTheDocument();
    expect(screen.getByText(/Reuters/)).toBeInTheDocument();
  });

  it("uses the amber attention color for an alert accent", () => {
    render(<FactWidget data={data} accent="alert" />);
    expect(screen.getByText("4")).toHaveClass("text-attention");
  });

  it("uses the primary cyan color by default", () => {
    render(<FactWidget data={data} />);
    expect(screen.getByText("4")).toHaveClass("text-primary");
  });

  it("omits the sources line when there are none", () => {
    render(<FactWidget data={{ ...data, sources: [] }} />);
    expect(screen.queryByText(/Reuters/)).not.toBeInTheDocument();
  });
});
