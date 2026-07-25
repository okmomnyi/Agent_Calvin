import { useAppStore } from "@/core/store";
import type { PendingApprovalKind } from "@/core/types";

// Everything waiting on Calvin, across every skill (core/session.py's pending_approvals()) --
// jobs, resale-listing purchase gates, flashcard candidates, deadlines, and proposed
// standing rules, each already reduced to one flat {kind, id, what, action} shape server
// side. Read-only: each kind is resolved by ITS OWN skill (job_hunter/spaced_rep/
// semester_planner/adaptive), which today only Telegram's inline buttons actually reach
// (skills/telegram_bot.py's handle_callback) -- there is no unified REST action for these
// yet, and POST /api/approvals/{id}/resolve is a DIFFERENT mechanism entirely (core/
// approvals.py's tiered proposal queue, its own id space). Wiring real per-kind actions here
// is real work, not a quick add; until then this panel tells the truth about what's pending
// and where to act on it instead of showing buttons that would silently 404.
const KIND_LABEL: Record<PendingApprovalKind, string> = {
  job: "Job",
  flip: "Resale",
  flashcard: "Flashcard",
  deadline: "Deadline",
  rule: "Rule",
};

// "flip" (resale purchase-gate) has no Telegram command yet -- left blank rather than
// guessing one, per the same rule that keeps this panel honest about what it can't do.
const KIND_TELEGRAM_HINT: Partial<Record<PendingApprovalKind, string>> = {
  job: "/jobs",
  flashcard: "/cards",
  deadline: "/deadlines",
  rule: "/rules",
};

export function ApprovalsList() {
  const approvals = useAppStore((s) => s.pendingApprovals);

  if (approvals.length === 0) return null;

  return (
    <section className="shrink-0 space-y-2 border-t border-line px-4 py-3">
      <p className="font-mono text-[10px] uppercase tracking-widest text-attention">
        Needs your call ({approvals.length})
      </p>
      <div className="max-h-40 space-y-1.5 overflow-y-auto">
        {approvals.map((a) => (
          <div
            key={`${a.kind}-${a.id}`}
            className="flex items-center justify-between gap-2 rounded-[var(--radius-control)] border border-line bg-surface px-3 py-1.5"
          >
            <div className="min-w-0">
              <span className="mr-2 font-mono text-[10px] uppercase tracking-widest text-text-mute">
                {KIND_LABEL[a.kind] ?? a.kind}
              </span>
              <span className="truncate text-sm text-text">{a.what}</span>
            </div>
            {KIND_TELEGRAM_HINT[a.kind] && (
              <span className="shrink-0 font-mono text-[10px] uppercase tracking-widest text-text-mute">
                {KIND_TELEGRAM_HINT[a.kind]}
              </span>
            )}
          </div>
        ))}
      </div>
      <p className="font-mono text-[10px] text-text-mute">
        Act on these from Telegram for now — quick actions here are coming.
      </p>
    </section>
  );
}
