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

// Mirrors core/session.py's pending_approvals() rows EXACTLY -- a cross-skill "what's
// waiting on Calvin" view spanning jobs/listings/flashcards/deadlines/rules, each already
// reduced to one flat shape server-side. NOT the same id space as core/approvals.py's
// ApprovalStore (POST /api/approvals/{id}/resolve) -- that's a separate, tiered
// proposal-queue mechanism with its own ids; resolving one of THESE rows means acting
// through the owning skill (job_hunter, spaced_rep, semester_planner, ...), which today only
// Telegram's inline buttons do (skills/telegram_bot.py's handle_callback). This panel is
// read-only until a matching REST action exists.
export type PendingApprovalKind = "job" | "flip" | "flashcard" | "deadline" | "rule";

export interface PendingApproval {
  kind: PendingApprovalKind;
  id: number;
  what: string;
  action: string;
}
