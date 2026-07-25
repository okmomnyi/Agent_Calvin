import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAppStore } from "@/core/store";
import { cn } from "@/lib/utils";
import type { JobListing } from "@/core/types";

// The scrollable job-listings card -- drafted/notified jobs awaiting a decision (GET
// /api/jobs, the same set skills/telegram_bot.py's /jobs command shows), browsable rather
// than squeezed into the compact approvals list. Read-only, same reasoning as
// ApprovalsList: applying/skipping is a job_hunter action Telegram's buttons already reach
// and this panel doesn't yet -- it's a browse view, not a second approval mechanism.
function scoreBandClass(score: number | null): string {
  if (score == null) return "text-text-mute";
  if (score >= 75) return "text-good";
  if (score >= 50) return "text-mid";
  return "text-bad";
}

function JobRow({ job }: { job: JobListing }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-line bg-surface px-3 py-2">
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 flex-1 break-words text-sm text-text">{job.title}</p>
        <span className={cn("shrink-0 font-mono text-xs font-semibold", scoreBandClass(job.score))}>
          {job.score ?? "—"}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-xs text-text-dim">{job.company}</p>
        {job.category && (
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-widest text-text-mute">
            {job.category}
          </span>
        )}
      </div>
    </div>
  );
}

export function JobsPanel() {
  const jobs = useAppStore((s) => s.jobs);

  return (
    <Card className="flex h-full flex-col overflow-hidden">
      <CardHeader className="shrink-0 flex-row items-center justify-between pb-2">
        <CardTitle>Job Listings</CardTitle>
        <span className="font-mono text-[10px] text-text-mute">{jobs.length}</span>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 space-y-2 overflow-y-auto overflow-x-hidden pt-0">
        {jobs.length === 0 ? (
          <p className="font-mono text-[11px] text-text-mute">
            No drafted or notified jobs right now.
          </p>
        ) : (
          jobs.map((j) => <JobRow key={j.id} job={j} />)
        )}
      </CardContent>
    </Card>
  );
}
