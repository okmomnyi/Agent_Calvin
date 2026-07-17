"""Email agent skill (Phase 2).

Three capabilities over Calvin's Gmail:
  * Hourly inbox cleanup — classify each new inbox message into one of six categories
    and archive promotions/newsletters/social under AgentOS/<Category> labels (never trash).
  * Daily 07:00 EAT digest — a grouped Telegram summary (action needed / FYI / ignored).
  * Reply drafting — turn an instruction into a Gmail DRAFT in Calvin's concise tone
    (never sends; approval/sending stays with Calvin per §0).
All processed messages are logged idempotently so restarts never double-process.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.config import get_settings
from core.gmail_client import GmailClient
from core.llm import LLMClient, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.email_agent")

CATEGORIES = ["promotion", "newsletter", "social", "job_related", "important", "personal"]
# Only these three are archived out of the inbox; the rest stay visible and just get labelled.
ARCHIVE_CATEGORIES = {"promotion", "newsletter", "social"}
ACTION_CATEGORIES = {"important", "job_related"}


class EmailAgentSkill(BaseSkill):
    name = "email_agent"

    def __init__(
        self,
        gmail: GmailClient | None = None,
        llm: LLMClient | None = None,
        memory: Memory | None = None,
        notify: Callable[[str], bool] | None = None,
    ) -> None:
        self._gmail = gmail
        self._llm = llm
        self._mem = memory
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
        since = _start_of_today_epoch()
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
        header = f"📬 {name}'s inbox digest — {time.strftime('%a %d %b')}"
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


def _start_of_today_epoch() -> float:
    lt = time.localtime()
    midnight = time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    return time.mktime(midnight)


def _extract_email(addr: str) -> str:
    """Pull the bare address out of a 'Name <a@b.com>' header value."""
    if "<" in addr and ">" in addr:
        return addr[addr.index("<") + 1: addr.index(">")].strip()
    return addr.strip()


SKILL = EmailAgentSkill()
