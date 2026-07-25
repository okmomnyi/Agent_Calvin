import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/core/transport";
import { authenticateWithPasskey } from "@/core/webauthnBrowser";
import { useAppStore } from "@/core/store";

// Everything waiting on Calvin, across every skill (core/session.py's pending_approvals()) --
// approve/deny reach the SAME ApprovalStore.resolve() Telegram's text-reply parser calls
// (kernel/app.py's own docstring on /api/approvals/{id}/resolve: "NOT a second approval
// mechanism"). A high-tier approve may come back 401 step_up_required; that's not a failure,
// it's the gate working -- get a fresh passkey assertion and retry once.
export function ApprovalsList() {
  const approvals = useAppStore((s) => s.pendingApprovals);
  const removePendingApproval = useAppStore((s) => s.removePendingApproval);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [errors, setErrors] = useState<Record<number, string>>({});

  async function resolve(id: number, approve: boolean): Promise<void> {
    setBusyId(id);
    setErrors((e) => ({ ...e, [id]: "" }));
    try {
      await api.resolveApproval(id, approve);
      removePendingApproval(id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401 && err.message === "step_up_required") {
        try {
          const options = await api.webauthnLoginOptions();
          const credential = await authenticateWithPasskey(options);
          await api.resolveApproval(id, approve, credential);
          removePendingApproval(id);
          return;
        } catch (stepUpErr) {
          setErrors((e) => ({ ...e, [id]: describeError(stepUpErr) }));
          return;
        }
      }
      setErrors((e) => ({ ...e, [id]: describeError(err) }));
    } finally {
      setBusyId(null);
    }
  }

  if (approvals.length === 0) return null;

  return (
    <section className="shrink-0 space-y-2 border-t border-line px-4 py-3">
      <p className="font-mono text-[10px] uppercase tracking-widest text-attention">
        Needs your approval ({approvals.length})
      </p>
      {approvals.map((a) => (
        <div
          key={a.id}
          className="flex items-center justify-between gap-2 rounded-[var(--radius-control)] border border-line bg-surface px-3 py-2"
        >
          <div className="min-w-0">
            <p className="truncate text-sm text-text">{a.title ?? `Action #${a.id}`}</p>
            {a.company != null && (
              <p className="truncate text-xs text-text-dim">{String(a.company)}</p>
            )}
            {errors[a.id] && <p className="text-xs text-bad">{errors[a.id]}</p>}
          </div>
          <div className="flex shrink-0 gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={busyId === a.id}
              onClick={() => void resolve(a.id, false)}
            >
              Deny
            </Button>
            <Button
              size="sm"
              variant="attention"
              disabled={busyId === a.id}
              onClick={() => void resolve(a.id, true)}
            >
              Approve
            </Button>
          </div>
        </div>
      ))}
    </section>
  );
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message || "Failed.";
  if (err instanceof Error) return err.message;
  return "Failed.";
}
