import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { TickerWidget } from "./TickerWidget";
import type { TickerWidgetData } from "@/core/stageTypes";

afterEach(cleanup);

describe("TickerWidget", () => {
  it("renders every item's symbol and signed change", () => {
    const data: TickerWidgetData = {
      type: "ticker",
      items: [
        { symbol: "BTC", price: 64000, change_pct: 2.1, as_of: 1 },
        { symbol: "OIL", price: 78.2, change_pct: -0.9, as_of: 1 },
      ],
    };
    render(<TickerWidget data={data} />);
    expect(screen.getByText("BTC")).toBeInTheDocument();
    expect(screen.getByText("+2.1%")).toBeInTheDocument();
    expect(screen.getByText("-0.9%")).toBeInTheDocument();
  });

  it("renders an empty ticker without crashing", () => {
    render(<TickerWidget data={{ type: "ticker", items: [] }} />);
    expect(screen.getByRole("list", { name: /market ticker/i })).toBeInTheDocument();
  });
});
