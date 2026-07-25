import { afterEach, describe, expect, it, vi } from "vitest";
import { callBridge, wireDesktopBridge } from "./desktopBridge";
import { useAppStore } from "./store";

describe("wireDesktopBridge", () => {
  afterEach(() => {
    delete (window as unknown as { __agentosBridgeEvent?: unknown }).__agentosBridgeEvent;
    useAppStore.setState({ hudState: "idle", micOn: false, turns: [] });
  });

  it("maps AssistantCore's MicState onto HudState", () => {
    wireDesktopBridge();
    const push = (window as unknown as { __agentosBridgeEvent: (e: unknown) => void })
      .__agentosBridgeEvent;

    push({ state: "thinking", mic_on: true, turns: [] });
    expect(useAppStore.getState().hudState).toBe("thinking");
    expect(useAppStore.getState().micOn).toBe(true);

    push({ state: "off", mic_on: false, turns: [] });
    expect(useAppStore.getState().hudState).toBe("idle");
    expect(useAppStore.getState().micOn).toBe(false);
  });

  it("pairs a you/agent turn into one chat exchange", () => {
    wireDesktopBridge();
    const push = (window as unknown as { __agentosBridgeEvent: (e: unknown) => void })
      .__agentosBridgeEvent;

    push({
      state: "off",
      mic_on: false,
      turns: [
        { who: "you", text: "what's the weather", actions: [] },
        { who: "agent", text: "28 and sunny", actions: [] },
      ],
    });

    const turns = useAppStore.getState().turns;
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe("what's the weather");
    expect(turns[0].reply).toBe("28 and sunny");
  });

  it("shows a dangling user turn with no reply yet rather than dropping it", () => {
    wireDesktopBridge();
    const push = (window as unknown as { __agentosBridgeEvent: (e: unknown) => void })
      .__agentosBridgeEvent;

    push({ state: "thinking", mic_on: true, turns: [{ who: "you", text: "hold on", actions: [] }] });

    const turns = useAppStore.getState().turns;
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe("hold on");
    expect(turns[0].reply).toBe("");
  });

  it("drops system rows from the chat view", () => {
    wireDesktopBridge();
    const push = (window as unknown as { __agentosBridgeEvent: (e: unknown) => void })
      .__agentosBridgeEvent;

    push({
      state: "off",
      mic_on: false,
      turns: [{ who: "system", text: "mic error", actions: [] }],
    });

    expect(useAppStore.getState().turns).toHaveLength(0);
  });
});

describe("callBridge", () => {
  afterEach(() => {
    delete (window as unknown as { pywebview?: unknown }).pywebview;
  });

  it("returns null when there is no pywebview bridge (e.g. the web shell)", async () => {
    await expect(callBridge("toggle_mic")).resolves.toBeNull();
  });

  it("calls the named bridge method with its args and returns the result", async () => {
    const toggle_mic = vi.fn().mockResolvedValue({ ok: true, mic_on: true });
    (window as unknown as { pywebview: { api: Record<string, unknown> } }).pywebview = {
      api: { toggle_mic },
    };
    const result = await callBridge("toggle_mic", 1, "x");
    expect(toggle_mic).toHaveBeenCalledWith(1, "x");
    expect(result).toEqual({ ok: true, mic_on: true });
  });

  it("returns null instead of throwing if the bridge method itself rejects", async () => {
    (window as unknown as { pywebview: { api: Record<string, unknown> } }).pywebview = {
      api: { hide: vi.fn().mockRejectedValue(new Error("gone")) },
    };
    await expect(callBridge("hide")).resolves.toBeNull();
  });
});
