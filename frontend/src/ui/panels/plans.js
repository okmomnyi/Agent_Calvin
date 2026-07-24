// Orchestrator plan/step view (core/orchestrator.py). /api/session only surfaces id/goal/
// status for the current plan (kernel/app.py's api_session handler deliberately trims it to
// that), so this panel shows what's actually there rather than a step list it doesn't have.
let el;

export function mount(container) {
  el = container;
}

export function render(session) {
  if (!el) return;
  el.replaceChildren();
  const plan = session?.current_plan;
  if (!plan) {
    const empty = document.createElement("p");
    empty.className = "empty dim";
    empty.textContent = "No active plan.";
    el.appendChild(empty);
    return;
  }
  const goal = document.createElement("div");
  goal.className = "plan-goal";
  goal.textContent = plan.goal;
  const status = document.createElement("span");
  status.className = `tag status-${plan.status}`;
  status.textContent = plan.status.replace("_", " ");
  const idLine = document.createElement("div");
  idLine.className = "dim mono";
  idLine.textContent = plan.id;
  el.append(goal, status, idLine);
}
