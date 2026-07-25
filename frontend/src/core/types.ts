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

// Mirrors core/session.py's Turn.to_dict() exactly -- `who` is derived client-side (a Turn
// row is one exchange, but the chat panel renders it as two entries: the user's text, then
// the reply).
export interface Turn {
  text: string;
  reply: string;
  channel: string;
  at: number;
  skill: string | null;
}

// Mirrors core/session.py's pending_approvals() rows (jobs today; the shape is deliberately
// loose -- title/company only ever come from job rows, other skills may add their own kind
// later, and the panel must degrade rather than break on a row it doesn't recognize).
export interface PendingApproval {
  id: number;
  title?: string;
  company?: string;
  [key: string]: unknown;
}
