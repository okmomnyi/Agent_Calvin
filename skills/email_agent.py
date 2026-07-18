"""Email agent skill (Phase 2).

Three capabilities over Calvin's Gmail:
  * Hourly inbox cleanup — classify each new inbox message into one of six categories
    and archive promotions/newsletters/social under AgentOS/<Category> labels.
  * User-requested cleanup — preview exact matches, require explicit confirmation, move
    them to recoverable Gmail Trash, and support undo. Never permanently delete.
  * Daily 07:00 EAT digest — a grouped Telegram summary (action needed / FYI / ignored).
  * Reply drafting — turn an instruction into a Gmail DRAFT in Calvin's concise tone
    (never sends; approval/sending stays with Calvin per §0).
All processed messages are logged idempotently so restarts never double-process.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from core.config import get_settings
from core.gmail_client import GmailClient
from core.llm import LLMClient, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
from core.time_context import local_now, start_of_local_day

log = get_logger("skills.email_agent")

CATEGORIES = ["promotion", "newsletter", "social", "job_related", "important", "personal"]
# Only these three are archived out of the inbox; the rest stay visible and just get labelled.
ARCHIVE_CATEGORIES = {"promotion", "newsletter", "social"}
ACTION_CATEGORIES = {"important", "job_related"}
_TRASH_SESSION_KEY = "email_agent.trash_session"
_LAST_TRASH_KEY = "email_agent.last_trash"


class EmailAgentSkill(BaseSkill):
    name = "email_agent"

    def __init__(
        self,
        gmail: GmailClient | None = None,
        llm: LLMClient | None = None,
        memory: Memory | None = None,
        notify: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._gmail = gmail
        self._llm = llm
        self._mem = memory
        self._now = clock
        # Injectable: anything that can reach Calvin's phone must be replaceable by a
        # test, or the suite texts him. See tests/test_voice.py's injection-point test.
        self._notify = notify or send_telegram

    # lazy singletons so importing the skill never requires Gmail creds / network
    @property
    def gmail(self) -> GmailClient:
        if self._gmail is None:
            self._gmail = GmailClient()
        return self._gmail

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    # ------------------------------------------------------------- skill wiring
    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "check": self.cleanup,
            "cleanup": self.cleanup,
            "digest": self.digest,
            "draft": self.draft,
            "trash": self.request_trash,
            "continue_trash": self.continue_trash,
            "restore": self.restore_last,
        }

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=["tone", "notifications"],
                             hard_invariants=["replies_are_drafts_only"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        # Note: the standalone 07:00 digest is subsumed by the Phase 13 unified morning
        # briefing (semester_planner), which embeds the inbox summary. Only hourly cleanup
        # is scheduled here; digest() remains callable on demand (/summarize, manage.py digest).
        return [
            ScheduledJob(id="email_agent.cleanup", func=self.cleanup, trigger="interval",
                         kwargs={"hours": 1}),
        ]

    # ------------------------------------------------------------- classification
    def classify_email(self, subject: str, sender: str, snippet: str) -> str:
        """Return one of CATEGORIES for a message (strict single-label)."""
        text = f"From: {sender}\nSubject: {subject}\nPreview: {snippet}"
        return self.llm.classify(
            text,
            CATEGORIES,
            instruction=(
                "Classify this email for a personal inbox. "
                "'job_related' = job boards, recruiters, applications, interviews. "
                "'important' = needs a human decision/response soon. "
                "'personal' = from a person Calvin knows. "
                "'promotion'/'newsletter'/'social' = bulk/automated mail."
            ),
        )

    # ------------------------------------------------------------- cleanup
    def cleanup(self, max_results: int = 50, **_: Any) -> CommandResult:
        """Classify new inbox messages; archive+label bulk mail. Idempotent per message."""
        try:
            ids = self.gmail.list_inbox(max_results=max_results)
        except Exception as exc:  # noqa: BLE001 - surfaces auth/network issues cleanly
            log.exception("Inbox listing failed")
            return CommandResult(text=f"Couldn't reach Gmail: {exc}", ok=False)

        counts: dict[str, int] = {c: 0 for c in CATEGORIES}
        processed = 0
        for msg_id in ids:
            if self.mem.email_seen(msg_id):
                continue
            msg = self.gmail.get_message(msg_id, fmt="metadata")
            subject = self.gmail.header(msg, "Subject")
            sender = self.gmail.header(msg, "From")
            snippet = msg.get("snippet", "")
            thread_id = msg.get("threadId")

            category = self.classify_email(subject, sender, snippet)
            action = "labelled"
            try:
                label_id = self.gmail.category_label(category)
                if category in ARCHIVE_CATEGORIES:
                    self.gmail.archive(msg_id, category_label_id=label_id)
                    action = "archived"
                else:
                    self.gmail.add_label(msg_id, label_id)
            except Exception:  # noqa: BLE001 - one bad message must not abort the run
                log.exception("Failed to label/archive message %s", msg_id)
                action = "error"

            self.mem.record_email(
                msg_id, thread_id=thread_id, category=category,
                subject=subject, sender=sender, action=action,
            )
            counts[category] += 1
            processed += 1
            log.info("email %s -> %s (%s)", msg_id, category, action)

        summary = ", ".join(f"{c}:{n}" for c, n in counts.items() if n) or "nothing new"
        return CommandResult(
            text=f"Inbox cleanup done. Processed {processed} new message(s): {summary}.",
            data={"processed": processed, "counts": counts},
        )

    # ------------------------------------------------------------- daily digest
    def digest(self, notify: bool = True, **_: Any) -> CommandResult:
        """Build and send the grouped daily digest from today's processed emails."""
        since = start_of_local_day(self._now())
        rows = self.mem.execute(
            "SELECT category, subject, sender, action FROM emails "
            "WHERE processed_at >= %s ORDER BY processed_at DESC",
            (since,),
        ).fetchall()

        action_items = [r for r in rows if r["category"] in ACTION_CATEGORIES]
        fyi_items = [r for r in rows if r["category"] in {"personal", "newsletter"}]
        ignored = sum(1 for r in rows if r["category"] in {"promotion", "social"})

        text = self._render_digest(action_items, fyi_items, ignored, total=len(rows))
        if notify:
            sent = self._notify(text)
            if not sent:
                log.warning("Digest built but Telegram not configured — returning text only.")
        return CommandResult(text=text, data={"action_needed": len(action_items),
                                              "fyi": len(fyi_items), "ignored": ignored})

    def _render_digest(self, action_items, fyi_items, ignored: int, total: int) -> str:
        name = get_settings().my_name
        header = f"📬 {name}'s inbox digest — {local_now(self._now()).strftime('%a %d %b')}"
        if total == 0:
            return f"{header}\n\nNothing new since yesterday. Inbox is quiet."

        # One LLM pass to write tight one-liners for the action-needed group.
        action_block = "None."
        if action_items:
            listing = "\n".join(
                f"- [{r['category']}] {r['subject'] or '(no subject)'} — {r['sender']}"
                for r in action_items[:15]
            )
            try:
                action_block = self.llm.chat(
                    "write",
                    [
                        {"role": "system", "content":
                            "Summarise these emails as a tight bullet list for a busy person. "
                            "One line each: what it is + what action it needs. No preamble."},
                        {"role": "user", "content": listing},
                    ],
                    max_tokens=400,
                )
            except Exception:  # noqa: BLE001 - fall back to the raw listing
                log.exception("Digest LLM summary failed; using raw listing")
                action_block = listing

        fyi_block = "\n".join(
            f"- {r['subject'] or '(no subject)'} — {r['sender']}" for r in fyi_items[:8]
        ) or "None."

        return (
            f"{header}\n\n"
            f"🔴 ACTION NEEDED ({len(action_items)}):\n{action_block}\n\n"
            f"🟡 FYI ({len(fyi_items)}):\n{fyi_block}\n\n"
            f"⚪ Ignored/archived: {ignored} promo/social message(s)."
        )

    # ------------------------------------------------------------- reply drafting
    def draft(self, instruction: str = "", msg_id: str = "", **_: Any) -> CommandResult:
        """Draft a reply to an email as a Gmail DRAFT (never sends).

        `msg_id` targets a specific message; otherwise the most recent action-needed email.
        """
        target_id = msg_id or self._latest_actionable_id()
        if not target_id:
            return CommandResult(text="No email to reply to — give me a message id or run cleanup first.",
                                 ok=False)
        try:
            msg = self.gmail.get_message(target_id, fmt="metadata")
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Couldn't fetch that email: {exc}", ok=False)

        subject = self.gmail.header(msg, "Subject")
        sender = self.gmail.header(msg, "From")
        message_id_hdr = self.gmail.header(msg, "Message-ID")
        snippet = msg.get("snippet", "")

        body = self._compose_reply(sender, subject, snippet, instruction)
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        to_addr = _extract_email(sender)

        try:
            draft = self.gmail.create_draft(
                to=to_addr, subject=reply_subject, body=body,
                thread_id=msg.get("threadId"), in_reply_to=message_id_hdr or None,
            )
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Couldn't create the draft: {exc}", ok=False)

        return CommandResult(
            text=f"Draft reply to {to_addr} saved (not sent). Review it in Gmail before sending.\n\n{body}",
            data={"draft_id": draft.get("id"), "to": to_addr},
        )

    def _compose_reply(self, sender: str, subject: str, snippet: str, instruction: str) -> str:
        name = get_settings().my_name
        sys = (
            f"You are drafting an email reply as {name}. Tone: direct, concise, human, warm "
            "but not effusive. No corporate filler. Sign off simply as the sender's first name. "
            "Only state facts given in the instruction — never invent commitments or details. "
            "(Persona-aware tone tuning arrives in Phase 4.)"
        )
        user = (
            f"Original email from {sender}, subject '{subject}'.\nPreview: {snippet}\n\n"
            f"Reply instruction from {name}: {instruction or '(no specific instruction — write a brief, appropriate reply)'}\n\n"
            "Write ONLY the reply body."
        )
        return self.llm.chat("write", [{"role": "system", "content": sys},
                                        {"role": "user", "content": user}], max_tokens=500)

    def _latest_actionable_id(self) -> str | None:
        row = self.mem.execute(
            "SELECT gmail_id FROM emails WHERE category IN ('important','job_related') "
            "ORDER BY processed_at DESC LIMIT 1"
        ).fetchone()
        return row["gmail_id"] if row else None

    # ------------------------------------------------------------- recoverable deletion
    def request_trash(self, query: str = "", max_results: int = 10, **_: Any) -> CommandResult:
        """Preview exact Gmail matches; never moves anything before a second confirmation."""
        query = _clean_delete_query(query)
        if not query:
            self.mem.kv_set(_TRASH_SESSION_KEY, json.dumps({
                "stage": "awaiting_query", "created_at": self._now()}))
            return CommandResult(
                text=("Which emails should I move to Trash? Give me a sender, subject, or age, "
                      "for example: ‘from LinkedIn’, ‘subject newsletter’, or ‘older than 30 days’."),
                data={"awaiting": "email_query"},
            )

        gmail_query = _gmail_query(query)
        try:
            ids = self.gmail.list_inbox(max_results=min(max(1, int(max_results)), 20),
                                        query=gmail_query)
            candidates = []
            for msg_id in ids:
                message = self.gmail.get_message(msg_id, fmt="metadata")
                candidates.append({
                    "id": msg_id,
                    "thread_id": message.get("threadId"),
                    "subject": self.gmail.header(message, "Subject") or "(no subject)",
                    "sender": self.gmail.header(message, "From") or "(unknown sender)",
                })
        except Exception as exc:  # noqa: BLE001
            return CommandResult(text=f"Couldn't search Gmail: {exc}", ok=False)

        if not candidates:
            self.mem.kv_set(_TRASH_SESSION_KEY, "")
            return CommandResult(text=f"No emails matched ‘{query}’. Nothing was changed.",
                                 data={"matches": []})

        self.mem.kv_set(_TRASH_SESSION_KEY, json.dumps({
            "stage": "awaiting_confirmation",
            "created_at": self._now(),
            "query": query,
            "messages": candidates,
        }))
        lines = [f"🗑 TRASH PREVIEW · {len(candidates)} message(s)",
                 "Nothing has been changed yet."]
        for index, item in enumerate(candidates, start=1):
            lines.append(f"{index}. {item['subject']}\n   From: {item['sender']}\n   ID: {item['id']}")
        lines.append("Say ‘confirm trash’ to move exactly these messages to Gmail Trash, or ‘cancel’.")
        return CommandResult(text="\n\n".join(lines),
                             data={"matches": candidates, "requires_confirmation": True})

    def continue_trash(self, text: str = "", **_: Any) -> CommandResult:
        raw = self.mem.kv_get(_TRASH_SESSION_KEY) or ""
        try:
            state = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            state = {}
        if not state:
            return self.request_trash(query=text)
        if self._now() - float(state.get("created_at", 0)) > 600:
            self.mem.kv_set(_TRASH_SESSION_KEY, "")
            return CommandResult(text="That trash preview expired after 10 minutes. Start again.", ok=False)
        if re.fullmatch(r"\s*(?:cancel|stop|never\s*mind)\s*", text, re.I):
            self.mem.kv_set(_TRASH_SESSION_KEY, "")
            return CommandResult(text="Email trash cancelled. Nothing was changed.")
        if state.get("stage") == "awaiting_query":
            return self.request_trash(query=text)
        if not re.fullmatch(r"\s*(?:confirm\s+trash|yes,?\s+trash\s+them)\s*", text, re.I):
            return CommandResult(
                text="Nothing changed. Say exactly ‘confirm trash’ to proceed, or ‘cancel’.",
                data={"requires_confirmation": True}, ok=False)

        moved, failed = [], []
        for item in state.get("messages", []):
            try:
                self.gmail.trash(item["id"])
                self.mem.record_email(
                    item["id"], thread_id=item.get("thread_id"), category="user_trashed",
                    subject=item.get("subject"), sender=item.get("sender"), action="trashed")
                self.mem.set_email_action(item["id"], "trashed")
                moved.append(item)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": item.get("id"), "error": str(exc)})
        self.mem.kv_set(_TRASH_SESSION_KEY, "")
        self.mem.kv_set(_LAST_TRASH_KEY, json.dumps({"at": self._now(), "messages": moved}))
        text_out = (f"Moved {len(moved)} message(s) to Gmail Trash. "
                    "They remain recoverable; say ‘undo email trash’ to restore them.")
        if failed:
            text_out += f" {len(failed)} message(s) failed and were left unchanged."
        return CommandResult(text=text_out, data={"trashed": moved, "failed": failed},
                             ok=bool(moved) and not failed)

    def restore_last(self, **_: Any) -> CommandResult:
        raw = self.mem.kv_get(_LAST_TRASH_KEY) or ""
        try:
            state = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            state = {}
        messages = state.get("messages", [])
        if not messages:
            return CommandResult(text="There is no recent AgentOS trash action to undo.", ok=False)
        restored, failed = 0, 0
        for item in messages:
            try:
                self.gmail.untrash(item["id"])
                self.mem.set_email_action(item["id"], "restored")
                restored += 1
            except Exception:  # noqa: BLE001
                failed += 1
        if not failed:
            self.mem.kv_set(_LAST_TRASH_KEY, "")
        return CommandResult(
            text=f"Restored {restored} message(s) from Gmail Trash."
                 + (f" {failed} could not be restored." if failed else ""),
            data={"restored": restored, "failed": failed}, ok=restored > 0 and failed == 0)


def _extract_email(addr: str) -> str:
    """Pull the bare address out of a 'Name <a@b.com>' header value."""
    if "<" in addr and ">" in addr:
        return addr[addr.index("<") + 1: addr.index(">")].strip()
    return addr.strip()


def _clean_delete_query(text: str) -> str:
    cleaned = (text or "").strip().strip(".?!")
    if re.fullmatch(r"(?:some\s+)?(?:of\s+)?(?:my\s+)?emails?", cleaned, re.I):
        return ""
    return cleaned


def _gmail_query(text: str) -> str:
    """Translate common spoken filters without asking an LLM to invent Gmail syntax."""
    cleaned = text.strip()
    if re.search(r"\b(?:from|subject|older_than|newer_than|category|before|after):", cleaned, re.I):
        return cleaned
    match = re.fullmatch(r"from\s+(.+)", cleaned, re.I)
    if match:
        return f"from:({match.group(1).strip()})"
    match = re.fullmatch(r"(?:with\s+)?subject\s+(.+)", cleaned, re.I)
    if match:
        return f"subject:({match.group(1).strip()})"
    match = re.fullmatch(r"older\s+than\s+(\d+)\s*(day|days|month|months|year|years)", cleaned, re.I)
    if match:
        suffix = {"day": "d", "days": "d", "month": "m", "months": "m",
                  "year": "y", "years": "y"}[match.group(2).lower()]
        return f"older_than:{match.group(1)}{suffix}"
    if cleaned.lower() in {"promotions", "promotion", "promotional emails"}:
        return "category:promotions"
    return cleaned


SKILL = EmailAgentSkill()
