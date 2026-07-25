// Jobs (Phase 3). There is no dedicated /api/jobs — this reads the `job` entries already
// present in /api/session's pending_approvals (core/session.py pulls them from `jobs` where
// status IN ('drafted','notified')). The Apply button sends the literal "apply to N" phrase,
// which is the one job action with a confirmed keyword route (core/intent.py's "approve"
// rule: `apply to (?P<t>[\d ,and]+)$`) — there's no equivalent keyword-routed skip, so this
// deliberately doesn't ship a Skip button that would silently fail to route.
import { sendCommand } from "../transcript.js";

let el;

export function mount(container) {
  el = container;
}

export function render(session) {
  if (!el) return;
  el.replaceChildren();
  const jobs = (session?.pending_approvals || []).filter((i) => i.kind === "job");
  if (!jobs.length) {
    const empty = document.createElement("p");
    empty.className = "empty dim";
    empty.textContent = "No jobs awaiting a decision.";
    el.appendChild(empty);
    return;
  }
  for (const job of jobs) {
    const row = document.createElement("div");
    row.className = "list-row";
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = job.what;
    const btn = document.createElement("button");
    btn.className = "btn ghost small";
    btn.textContent = "Apply";
    btn.addEventListener("click", () => sendCommand(`apply to ${job.id}`));
    row.append(what, btn);
    el.appendChild(row);
  }
}
