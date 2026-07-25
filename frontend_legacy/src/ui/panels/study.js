// Study (Phases 9-12). Candidate flashcards come straight from /api/session's
// pending_approvals (kind "flashcard"); "Quiz me" sends the literal phrase core/intent.py's
// `quiz` rule matches (`\bquiz me\b`).
import { sendCommand } from "../transcript.js";

let el, listEl, outputEl;

export function mount(container) {
  el = container;
  el.replaceChildren();
  listEl = document.createElement("div");
  const btn = document.createElement("button");
  btn.className = "btn ghost small";
  btn.textContent = "Quiz me";
  btn.addEventListener("click", async () => {
    outputEl.classList.remove("dim");
    outputEl.textContent = "…";
    try {
      const reply = await sendCommand("quiz me");
      outputEl.textContent = reply.text;
    } catch (e) {
      outputEl.textContent = "⚠ " + e.message;
    }
  });
  outputEl = document.createElement("div");
  outputEl.className = "panel-output dim";
  outputEl.textContent = "Quiz replies appear here.";
  el.append(listEl, btn, outputEl);
}

export function render(session) {
  if (!listEl) return;
  listEl.replaceChildren();
  const cards = (session?.pending_approvals || []).filter((i) => i.kind === "flashcard");
  if (!cards.length) {
    const empty = document.createElement("p");
    empty.className = "empty dim";
    empty.textContent = "No candidate flashcards awaiting review.";
    listEl.appendChild(empty);
    return;
  }
  for (const card of cards) {
    const row = document.createElement("div");
    row.className = "list-row";
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = card.what;
    row.appendChild(what);
    listEl.appendChild(row);
  }
}
