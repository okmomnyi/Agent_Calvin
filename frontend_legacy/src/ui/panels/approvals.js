// "Waiting on you" (A3): the cross-skill view SessionStore.pending_approvals() already
// exposes, plus the active plan when it's blocked on a yes/no. This is read-heavy by
// design — session.py's pending_approvals() is itself read-only.
import { sendCommand } from "../transcript.js";

let el;

export function mount(container) {
  el = container;
}

export function render(session) {
  if (!el) return;
  el.replaceChildren();

  const items = [...(session?.pending_approvals || [])];
  const plan = session?.current_plan;
  const planBlocked = plan && ["awaiting_approval", "paused"].includes(plan.status);

  if (planBlocked) {
    el.appendChild(planRow(plan));
  }
  if (!items.length && !planBlocked) {
    const empty = document.createElement("p");
    empty.className = "empty dim";
    empty.textContent = "Nothing waiting on you.";
    el.appendChild(empty);
    return;
  }
  for (const item of items) {
    el.appendChild(approvalRow(item));
  }
}

function planRow(plan) {
  const row = document.createElement("div");
  row.className = "approval-row";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "plan";
  const what = document.createElement("span");
  what.className = "what";
  what.textContent = plan.goal;
  const actions = document.createElement("span");
  actions.className = "row-actions";
  // "yes" / "abort" are the exact literal phrases core/orchestrator.py's reply parser
  // matches (_YES_RE / _ABORT_RE) — anything looser risks a silent no-op.
  actions.append(
    actionButton("Approve", () => sendCommand("yes")),
    actionButton("Abort", () => sendCommand("abort")),
  );
  row.append(tag, what, actions);
  return row;
}

function approvalRow(item) {
  const row = document.createElement("div");
  row.className = "approval-row";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = item.kind;
  const what = document.createElement("span");
  what.className = "what";
  what.textContent = item.what;
  const action = document.createElement("span");
  action.className = "action dim";
  action.textContent = item.action;
  row.append(tag, what, action);
  return row;
}

function actionButton(label, onClick) {
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}
