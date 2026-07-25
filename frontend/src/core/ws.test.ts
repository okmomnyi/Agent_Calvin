import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoffDelay, VoiceSocket } from "./ws";

vi.mock("./transport", () => ({
  api: { wsTicket: vi.fn() },
}));

// Minimal WebSocket stand-in -- readyState is driven by the test, not by real network
// events, since jsdom has no WS server to connect to.
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  static instances: FakeWebSocket[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  simulateOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  simulateClose() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  // Real WebSocket#close() -- VoiceSocket.close() calls this directly.
  close() {
    this.simulateClose();
  }
}

describe("backoffDelay", () => {
  it("is bounded within [half, full) of the exponential step, floored by attempt 0's base", () => {
    expect(backoffDelay(0, () => 0)).toBe(250);
    expect(backoffDelay(0, () => 0.999999)).toBeCloseTo(500, 0);
  });

  it("caps at MAX_BACKOFF_MS regardless of how large the attempt count grows", () => {
    expect(backoffDelay(10, () => 0)).toBe(7500);
    expect(backoffDelay(10, () => 0.999999)).toBeCloseTo(15000, 0);
  });
});

describe("VoiceSocket", () => {
  beforeEach(async () => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.useFakeTimers();
    const { api } = await import("./transport");
    vi.mocked(api.wsTicket).mockReset().mockResolvedValue({ ticket: "raw-ticket" });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("mints a ticket and opens against /ws/voice with it in the query string", async () => {
    const onStatusChange = vi.fn();
    const socket = new VoiceSocket({ onMessage: vi.fn(), onStatusChange });
    await socket.connect();

    expect(onStatusChange).toHaveBeenCalledWith("connecting");
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toContain("ticket=raw-ticket");

    FakeWebSocket.instances[0].simulateOpen();
    expect(onStatusChange).toHaveBeenCalledWith("open");
  });

  it("only sends once the socket reports OPEN", async () => {
    const socket = new VoiceSocket({ onMessage: vi.fn(), onStatusChange: vi.fn() });
    await socket.connect();
    const ws = FakeWebSocket.instances[0];

    expect(socket.send("hello")).toBe(false);
    ws.simulateOpen();
    expect(socket.send("hello")).toBe(true);
    expect(ws.sent).toEqual([JSON.stringify({ text: "hello" })]);
  });

  it("reconnects with backoff after an unexpected close, minting a fresh ticket", async () => {
    const { api } = await import("./transport");
    const socket = new VoiceSocket({ onMessage: vi.fn(), onStatusChange: vi.fn() });
    await socket.connect();
    FakeWebSocket.instances[0].simulateOpen();
    FakeWebSocket.instances[0].simulateClose();

    expect(api.wsTicket).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.wsTicket).toHaveBeenCalledTimes(2);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("stops reconnecting once closed deliberately", async () => {
    const { api } = await import("./transport");
    const socket = new VoiceSocket({ onMessage: vi.fn(), onStatusChange: vi.fn() });
    await socket.connect();
    FakeWebSocket.instances[0].simulateOpen();
    socket.close(); // sets stopped=true, then calls ws.close() itself

    await vi.advanceTimersByTimeAsync(20000);
    expect(api.wsTicket).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
