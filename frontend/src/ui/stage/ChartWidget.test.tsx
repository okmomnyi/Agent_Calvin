import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ChartWidget } from "./ChartWidget";
import type { ChartWidgetData } from "@/core/stageTypes";

afterEach(cleanup);

function chart(overrides: Partial<ChartWidgetData> = {}): ChartWidgetData {
  return {
    type: "chart",
    asset: "Gold",
    klass: "commodity",
    range: "1d",
    series: [
      { t: 1000, v: 2000 },
      { t: 2000, v: 2050 },
    ],
    as_of: 2000,
    delayed_label: "~15m delayed (free feed)",
    source: "yahoo",
    ...overrides,
  };
}

describe("ChartWidget", () => {
  it("always shows the as_of, delayed label, and source together -- the honesty line", () => {
    render(<ChartWidget data={chart()} />);
    const footer = screen.getByText(/~15m delayed \(free feed\)/);
    expect(footer.textContent).toContain("yahoo");
    expect(footer.textContent).toMatch(/AS OF/);
  });

  it("renders the asset name and klass", () => {
    render(<ChartWidget data={chart({ asset: "Bitcoin", klass: "crypto" })} />);
    expect(screen.getByText(/Bitcoin/)).toBeInTheDocument();
    expect(screen.getByText(/crypto/)).toBeInTheDocument();
  });

  it("does not crash on a single-point series", () => {
    render(<ChartWidget data={chart({ series: [{ t: 1, v: 1 }] })} />);
    expect(screen.getByRole("img", { name: /price chart/i })).toBeInTheDocument();
  });

  it("does not crash on an empty series", () => {
    render(<ChartWidget data={chart({ series: [] })} />);
    expect(screen.getByRole("img", { name: /price chart/i })).toBeInTheDocument();
  });

  it("real-time crypto carries its own distinct delayed label from a delayed fx/commodity chart", () => {
    render(<ChartWidget data={chart({ source: "coingecko", delayed_label: "real-time" })} />);
    expect(screen.getByText(/real-time/)).toBeInTheDocument();
  });
});
