// Shared across the HUD, the wave visualizer, and the app-wide store.
export type HudState = "idle" | "listening" | "thinking" | "speaking" | "awaiting-approval" | "error";

export const HUD_STATES: readonly HudState[] = [
  "idle",
  "listening",
  "thinking",
  "speaking",
  "awaiting-approval",
  "error",
];
