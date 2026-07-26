import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { StageCanvas } from "./StageCanvas";
import { useAppStore } from "@/core/store";
import { stageAnimationBudget, MAX_ANIMATING_WIDGETS } from "./animationBudget";
import type { ArticlesWidgetData, ChartWidgetData, FactWidgetData, StageDirective, TickerWidgetData } from "@/core/stageTypes";

function resetStore() {
  useAppStore.setState({
    hudState: "idle",
    stageDirective: null,
    pinnedWidget: null,
  });
}

afterEach(() => {
  cleanup();
  resetStore();
  stageAnimationBudget.reset();
  vi.unstubAllGlobals();
});

beforeEach(resetStore);

const chart: ChartWidgetData = {
  type: "chart", asset: "Gold", klass: "commodity", range: "1d",
  series: [{ t: 1, v: 1 }, { t: 2, v: 2 }], as_of: 2, delayed_label: "~15m delayed", source: "yahoo",
};
const fact: FactWidgetData = { type: "fact", title: "CORROBORATION", stat: "4", sub: "sources", sources: ["Reuters"] };
const articles: ArticlesWidgetData = {
  type: "articles", topic: "World",
  items: [{ title: "A story", source: "BBC", url: "https://x.test/1", published: 0 }],
};
const ticker: TickerWidgetData = { type: "ticker", items: [{ symbol: "BTC", price: 1, change_pct: 1, as_of: 1 }] };

function renderStage(directive: StageDirective | null) {
  useAppStore.setState({ stageDirective: directive });
  return render(<StageCanvas hudState="idle" micLevel={0} micOn={false} shell="web" />);
}

describe("StageCanvas", () => {
  it("degrades to the calm idle HUD when there is no directive", () => {
    renderStage(null);
    expect(screen.queryByTestId("stage-active")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^HUD state/i })).toBeInTheDocument();
  });

  it("degrades to idle for an explicit idle directive (focus null, no widgets)", () => {
    renderStage({ focus: null, transition: "settle", widgets: [] });
    expect(screen.queryByTestId("stage-active")).not.toBeInTheDocument();
  });

  it("renders the focus label and each widget in its own slot", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart, fact, articles, ticker] });
    expect(screen.getByTestId("stage-active")).toBeInTheDocument();
    expect(screen.getByText("Gold")).toBeInTheDocument();
    expect(screen.getByText("CORROBORATION")).toBeInTheDocument();
    expect(screen.getByText("World")).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
  });

  it("shows the headline only when the directive carries one", () => {
    renderStage({ focus: "Gold", transition: "bloom", headline: "Gold is up", widgets: [chart] });
    expect(screen.getByText("Gold is up")).toBeInTheDocument();
  });

  it("an un-buildable widget being absent from the directive simply isn't rendered -- no placeholder", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    expect(screen.queryByText("CORROBORATION")).not.toBeInTheDocument();
    expect(screen.queryByText("World")).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------- pin
  it("a pin button freezes a widget so a later directive doesn't replace it", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    fireEvent.click(screen.getByRole("button", { name: /pin chart/i }));

    const differentChart: ChartWidgetData = { ...chart, asset: "Bitcoin", series: [{ t: 9, v: 9 }] };
    act(() => {
      useAppStore.getState().setStageDirective({ focus: "Bitcoin", transition: "bloom", widgets: [differentChart] });
    });

    expect(screen.getByRole("heading", { name: /Gold/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Bitcoin/i })).not.toBeInTheDocument();
  });

  it("a pinned widget survives a directive that omits that widget type entirely", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    fireEvent.click(screen.getByRole("button", { name: /pin chart/i }));

    act(() => {
      useAppStore.getState().setStageDirective({ focus: "World", transition: "bloom", widgets: [articles] });
    });

    expect(screen.getByText(/Gold/)).toBeInTheDocument(); // pinned chart still there
    expect(screen.getByRole("heading", { name: "World" })).toBeInTheDocument(); // new articles widget also there
  });

  it("unpinning lets the next directive replace the widget again", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    fireEvent.click(screen.getByRole("button", { name: /pin chart/i }));
    fireEvent.click(screen.getByRole("button", { name: /unpin chart/i }));

    const differentChart: ChartWidgetData = { ...chart, asset: "Bitcoin" };
    act(() => {
      useAppStore.getState().setStageDirective({ focus: "Bitcoin", transition: "bloom", widgets: [differentChart] });
    });

    expect(screen.queryByRole("heading", { name: /Gold/i })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Bitcoin/i })).toBeInTheDocument();
  });

  it("voice pin (pinCurrentWidget) picks the highest-priority widget currently shown", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [articles, chart] });
    act(() => useAppStore.getState().pinCurrentWidget());
    expect(useAppStore.getState().pinnedWidget?.type).toBe("chart");
  });

  // -------------------------------------------------------------- ttl idle-return
  it("returns to idle on its own after ttl_s, with no server round-trip", () => {
    vi.useFakeTimers();
    renderStage({ focus: "Gold", transition: "bloom", ttl_s: 5, widgets: [chart] });
    expect(screen.getByTestId("stage-active")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(5000));

    expect(screen.queryByTestId("stage-active")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("does not idle-return when no ttl_s is set", () => {
    vi.useFakeTimers();
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByTestId("stage-active")).toBeInTheDocument();
    vi.useRealTimers();
  });

  // -------------------------------------------------------------- reduced motion
  it("requests no animation slot at all under prefers-reduced-motion", () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
      matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart, fact, articles] });
    expect(stageAnimationBudget.activeCount).toBe(0);
  });

  // -------------------------------------------------------------- animation cap
  it("never exceeds the animating-widget cap even with every slot filled at once", () => {
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart, fact, articles, ticker] });
    expect(stageAnimationBudget.activeCount).toBeLessThanOrEqual(MAX_ANIMATING_WIDGETS);
  });

  // -------------------------------------------------------------- event-driven only
  it("never rotates on its own -- the same directive stays rendered indefinitely without a new event", () => {
    vi.useFakeTimers();
    renderStage({ focus: "Gold", transition: "bloom", widgets: [chart] });
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByText("Gold")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("source contains no interval-driven scene rotation (the prototype's auto-cycle is not ported)", () => {
    const source = readFileSync(path.join(__dirname, "StageCanvas.tsx"), "utf-8");
    expect(source).not.toMatch(/setInterval/);
  });

  it("pin controls never call into the api/transport layer -- pure client-side state", () => {
    const source = readFileSync(path.join(__dirname, "StageCanvas.tsx"), "utf-8");
    expect(source).not.toMatch(/from ["']@\/core\/transport["']/);
  });
});
