"""Telegram bot — full remote control of AgentOS (Phase 8).

Single authorized chat id. Commands (/status /jobs /approve /ask /find /form /prep /mock
/draft /facts /events /cv /summarize /remember /forget /instructions /voiceoff /voiceon),
inline Apply/Skip/Details buttons on the job digest, free text routed through the SAME
intent engine as voice, and voice notes transcribed on the droplet (faster-whisper) and
routed identically — the reliable "call it from my phone" path (Phase 7 phone access).

BotCore holds all the testable logic (authorization, status, callbacks, routing); the
python-telegram-bot handlers at the bottom are thin async wrappers. PTB is imported lazily
so importing this module never requires the library or starts polling.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from kernel.registry import SkillRegistry

log = get_logger("skills.telegram_bot")

# command -> (skill, action, arg_key). Special commands (status/jobs/approve/voice) handled separately.
COMMAND_MAP: dict[str, tuple[str, str, str | None]] = {
    "ask": ("persona", "answer", "question"),
    "find": ("research", "search", "query"),
    "prep": ("interview_prep", "prep", "company"),
    "mock": ("interview_prep", "mock", "company"),
    "draft": ("email_agent", "draft", "instruction"),
    "form": ("form_assist", "answer", "content"),
    "remember": ("persona", "remember", "instruction"),
    "forget": ("persona", "forget", "instruction"),
    "instructions": ("persona", "instructions", None),
    "facts": ("persona", "facts", "category"),
    "events": ("event_scout", "find", "tag"),
    "cv": ("cv_tailor", "view", None),
    "digest": ("email_agent", "digest", None),
    "surge": ("spaced_rep", "surge", "unit"),
    "reviewreport": ("spaced_rep", "report", None),
    "tutor": ("code_tutor", "start", "topic"),
    "explain": ("code_tutor", "explain", "topic"),
    "drill": ("code_tutor", "drill", "topic"),
    "socratic": ("code_tutor", "socratic", "question"),
    "mocklab": ("code_tutor", "mocklab", "topic"),
    "briefing": ("semester_planner", "briefing", None),
    "plan": ("semester_planner", "plan", None),
    "cram": ("semester_planner", "cram", "unit"),
    "due": ("semester_planner", "due", None),
    "rules": ("adaptive", "candidates", None),
    "retro": ("adaptive", "retro", "answer"),
    "contracts": ("adaptive", "contracts", None),
}

HELP = (
    "AgentOS remote control:\n"
    "/status — system snapshot\n"
    "/jobs — latest job matches with Apply/Skip buttons\n"
    "/approve 1,3 — apply to jobs by id\n"
    "/ask <question> — answer as you (from verified facts)\n"
    "/find <query> — web research (cited)\n"
    "/form <text|url> — build an answer sheet (never submits)\n"
    "/prep <company> · /mock <company> — interview prep & rehearsal\n"
    "/draft <instruction> — draft an email reply (never sends)\n"
    "/facts — browse persona facts\n"
    "/quiz [unit] · /cards — spaced-repetition review & card approval\n"
    "/tutor <mode> <topic> — explain/drill/socratic/mock lab · /explain <topic>\n"
    "/briefing · /plan · /due · /cram <unit> — semester command center\n"
    "/deadline <YYYY-MM-DD> <title> · /deadlines — add/confirm deadlines\n"
    "/remember <rule> · /forget <rule> · /instructions\n"
    "/events [tag] · /tags add|remove <tag> — free events matching your interests\n"
    "/cv [update|tailor <JD>|facts] — CV tailoring & ATS optimization\n"
    "/rules · /retro · /contracts — patterns I've noticed & what each skill may read\n"
    "/summarize <thing> · /voiceoff · /voiceon\n"
    "Send a voice note or just type — it routes like everything else."
)

_MOCK_KEY = "interview_prep.mock"
_QUIZ_KEY = "spaced_rep.session"
_TUTOR_KEY = "code_tutor.session"
_TRASH_KEY = "email_agent.trash_session"


def parse_callback(data: str) -> tuple[str | None, int | None]:
    """Parse job callback data 'j:<action>:<job_id>' -> (action, job_id)."""
    parts = (data or "").split(":")
    if len(parts) == 3 and parts[0] == "j":
        try:
            return parts[1], int(parts[2])
        except ValueError:
            return None, None
    return None, None


def job_buttons(jobs: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    """Pure keyboard spec: for each job, a row of (label, callback_data) tuples."""
    rows = []
    for j in jobs:
        jid = j["id"]
        rows.append([("✅ Apply", f"j:apply:{jid}"), ("📄 Tailor CV", f"j:tailor:{jid}"),
                     ("⏭ Skip", f"j:skip:{jid}"), ("🔎 Details", f"j:details:{jid}")])
    return rows


GRADE_BUTTONS = [[("Again", "q:grade:again"), ("Hard", "q:grade:hard"),
                  ("Good", "q:grade:good"), ("Easy", "q:grade:easy")]]
REVEAL_BUTTON = [[("👁 Reveal answer", "q:reveal")]]


def candidate_buttons(cards: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    """Pure keyboard spec for candidate flashcards: Approve/Reject per card."""
    return [[("✅ Approve", f"c:approve:{c['id']}"), ("🗑 Reject", f"c:reject:{c['id']}")]
            for c in cards]


def deadline_buttons(deadlines: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    """Pure keyboard spec for pending deadlines: Confirm/Discard per deadline."""
    return [[("✅ Confirm", f"d:confirm:{d['id']}"), ("🗑 Discard", f"d:reject:{d['id']}")]
            for d in deadlines]


def rule_buttons(proposals: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    """Pure keyboard spec for proposed standing rules (Phase 20): Confirm/Reject/Not now."""
    return [[("✅ Confirm", f"r:confirm:{p['id']}"), ("🚫 Reject", f"r:decline:{p['id']}"),
             ("🕓 Not now", f"r:later:{p['id']}")] for p in proposals]


def event_buttons(events: list[dict[str, Any]]) -> list[list[tuple[str, str]]]:
    """Pure keyboard spec for events: Interested/Skip per event."""
    return [[("⭐ Interested", f"e:interested:{e['id']}"), ("⏭ Skip", f"e:skip:{e['id']}")]
            for e in events]


def _start_of_today() -> float:
    lt = time.localtime()
    return time.mktime(time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)))


class BotCore:
    """All bot logic, decoupled from python-telegram-bot for testability."""

    def __init__(self, registry: SkillRegistry | None = None, memory: Memory | None = None,
                 transcribe: Callable[[str], str] | None = None) -> None:
        self.settings = get_settings()
        self.mem = memory or get_memory()
        self.registry = registry or SkillRegistry()
        if registry is None:
            self.registry.discover()
        self._transcribe = transcribe
        self.started = time.time()

    # ------------------------------------------------------------- auth
    def is_authorized(self, chat_id: int | str) -> bool:
        allowed = self.settings.telegram_chat_id
        return bool(allowed) and str(chat_id) == str(allowed)

    # ------------------------------------------------------------- dispatch helpers
    def _dispatch(self, skill: str, action: str, payload: dict[str, Any]) -> str:
        from core.intent import Intent

        intent = Intent(name=action, skill=skill, action=action, args=payload)
        return self.registry.dispatch_intent(intent).text

    def run_command(self, cmd: str, arg: str = "") -> str:
        """Handle a slash command (without the leading '/'). Returns reply text."""
        cmd = cmd.lstrip("/").lower()
        if cmd in ("start", "help"):
            return HELP
        if cmd == "status":
            return self.status_text()
        if cmd == "approve":
            ids = [int(n) for n in arg.replace(",", " ").split() if n.strip().isdigit()]
            return self._dispatch("job_hunter", "approve", {"selection": ids})
        if cmd in ("voiceoff", "voiceon"):
            return self._dispatch("voice", "mute" if cmd == "voiceoff" else "unmute", {})
        if cmd == "summarize":
            return self.route_text(f"summarize {arg}")
        if cmd in COMMAND_MAP:
            skill, action, arg_key = COMMAND_MAP[cmd]
            payload = {arg_key: arg} if arg_key else {}
            return self._dispatch(skill, action, payload)
        return f"Unknown command /{cmd}. Try /help."

    def route_text(self, text: str) -> str:
        """Free text: continue an active mock/quiz if one is running, else route via intent engine."""
        if not text.startswith("/"):
            if self.mem.kv_get(_TRASH_KEY):  # "confirm trash" / "cancel" / a follow-up filter
                return self._dispatch("email_agent", "continue_trash", {"text": text})
            if self.mem.kv_get(_MOCK_KEY):
                return self._dispatch("interview_prep", "mock_answer", {"answer": text})
            if self.mem.kv_get(_QUIZ_KEY):   # voice/typed quiz answer -> judged
                return self._dispatch("spaced_rep", "quiz_answer", {"answer": text})
            if self.mem.kv_get(_TUTOR_KEY):  # drill solution / socratic answer / lab submit
                return self._dispatch("code_tutor", "continue", {"text": text})
        _intent, result = self.registry.handle_command(text)
        return result.text

    def quiz_active(self) -> bool:
        return bool(self.mem.kv_get(_QUIZ_KEY))

    def run_command_raw(self, skill: str, action: str, payload: dict[str, Any]) -> str:
        """Dispatch straight to a skill/action (used by handlers that need custom keyboards)."""
        return self._dispatch(skill, action, payload)

    def candidates(self) -> list[dict[str, Any]]:
        """Candidate flashcards awaiting approval (for the /cards keyboard)."""
        skill = self.registry.get("spaced_rep")
        if skill is None:
            return []
        return skill.list_candidates().data.get("candidates", [])

    def pending_deadlines(self) -> list[dict[str, Any]]:
        """Email-extracted deadlines awaiting confirmation (for the /deadlines keyboard)."""
        return [{"id": d["id"], "title": d["title"], "unit": d["unit"], "type": d["type"]}
                for d in self.mem.pending_deadlines()]

    def events(self, tag: str = "") -> list[dict[str, Any]]:
        """Ranked free events (for the /events keyboard)."""
        skill = self.registry.get("event_scout")
        if skill is None:
            return []
        return skill.find(tag=tag).data.get("events", [])

    # ------------------------------------------------------------- jobs + callbacks
    def jobs_payload(self) -> tuple[str, list[dict[str, Any]]]:
        """Return (header text, jobs) for drafted/notified jobs awaiting approval."""
        rows = self.mem.jobs_by_status("notified", limit=10) + self.mem.jobs_by_status("drafted", limit=10)
        jobs = [{"id": r["id"], "title": r["title"], "company": r["company"],
                 "score": r["score"], "category": r["category"],
                 "apply_kind": r["apply_kind"], "apply_target": r["apply_target"]} for r in rows]
        if not jobs:
            return "No job matches awaiting approval. Run a hunt or check back after the next scan.", []
        return f"💼 {len(jobs)} job(s) awaiting your call:", jobs

    def handle_callback(self, data: str) -> str:
        """Handle an inline-button press. Returns reply text."""
        parts = (data or "").split(":")
        kind = parts[0] if parts else ""
        if kind == "q":                                   # quiz reveal/grade
            if parts[1] == "reveal":
                return self._dispatch("spaced_rep", "reveal", {})
            if parts[1] == "grade" and len(parts) == 3:
                return self._dispatch("spaced_rep", "grade", {"grade": parts[2]})
            return "Unrecognized action."
        if kind == "c" and len(parts) == 3:               # candidate card approve/reject
            try:
                cid = int(parts[2])
            except ValueError:
                return "Unrecognized action."
            act = "approve_card" if parts[1] == "approve" else "reject_card"
            return self._dispatch("spaced_rep", act, {"card_id": cid})
        if kind == "d" and len(parts) == 3:               # pending deadline confirm/discard
            try:
                did = int(parts[2])
            except ValueError:
                return "Unrecognized action."
            act = "confirm_deadline" if parts[1] == "confirm" else "reject_deadline"
            return self._dispatch("semester_planner", act, {"deadline_id": did})
        if kind == "r" and len(parts) == 3:               # proposed standing rule
            try:
                sid = int(parts[2])
            except ValueError:
                return "Unrecognized action."
            act = {"confirm": "confirm", "decline": "decline", "later": "not_now"}.get(parts[1])
            if not act:
                return "Unrecognized action."
            return self._dispatch("adaptive", act, {"signal_id": sid})
        if kind == "e" and len(parts) == 3:               # event interested/skip
            try:
                eid = int(parts[2])
            except ValueError:
                return "Unrecognized action."
            act = "interested" if parts[1] == "interested" else "skip"
            return self._dispatch("event_scout", act, {"event_id": eid})
        action, job_id = parse_callback(data)
        if action is None or job_id is None:
            return "Unrecognized action."
        if action == "apply":
            job = self.mem.get_job(job_id)
            if job and job.get("category"):
                self.mem.log_signal("job_hunter", "job_skipped", job["category"], contradicts=True)
            return self._dispatch("job_hunter", "approve", {"selection": [job_id]})
        if action == "skip":
            job = self.mem.get_job(job_id)
            self.mem.set_job_status(job_id, "skipped")
            if job and job.get("category"):      # passive signal (Phase 20) — never acts
                self.mem.log_signal("job_hunter", "job_skipped", job["category"])
            return f"⏭ Skipped job {job_id}."
        if action == "tailor":
            return self._dispatch("cv_tailor", "tailor", {"job_id": job_id})
        if action == "details":
            job = self.mem.get_job(job_id)
            if not job:
                return f"Job {job_id} not found."
            return (f"[{job['id']}] {job['title']} @ {job['company']}\n"
                    f"Score {job['score']} · {job['category']} · {job['apply_kind']}\n"
                    f"{job['apply_target'] or ''}\n\n{(job['cover_text'] or '')[:1500]}")
        return "Unrecognized action."

    # ------------------------------------------------------------- status
    def status_text(self) -> str:
        since = _start_of_today()
        q = self.mem.execute
        jobs_today = q("SELECT COUNT(*) c FROM jobs WHERE first_seen>=%s", (since,)).fetchone()["c"]
        awaiting = q("SELECT COUNT(*) c FROM jobs WHERE status IN ('drafted','notified')").fetchone()["c"]
        apps = q("SELECT COUNT(*) c FROM applications").fetchone()["c"]
        emails_today = q("SELECT COUNT(*) c FROM emails WHERE processed_at>=%s", (since,)).fetchone()["c"]
        uptime_h = (time.time() - self.started) / 3600
        return (f"🟢 AgentOS status\n"
                f"Skills online: {len(self.registry.skills)}\n"
                f"Jobs found today: {jobs_today} (awaiting approval: {awaiting})\n"
                f"Applications tracked: {apps}\n"
                f"Emails processed today: {emails_today}\n"
                f"Bot uptime: {uptime_h:.1f}h")

    # ------------------------------------------------------------- voice notes
    def transcribe(self, ogg_path: str) -> str:
        """Transcribe a downloaded voice note (faster-whisper on the droplet)."""
        if self._transcribe is not None:
            return self._transcribe(ogg_path)
        try:
            from faster_whisper import WhisperModel  # heavy; droplet-side only

            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(ogg_path, language="en")
            return " ".join(s.text for s in segments).strip()
        except Exception:  # noqa: BLE001
            log.exception("voice-note transcription failed")
            return ""


# ==================================================================== PTB glue
def _chunks(text: str, size: int = 4000):
    for i in range(0, len(text) or 1, size):
        yield text[i:i + size] or " "


def build_application(core: BotCore | None = None):
    """Build the python-telegram-bot Application with all handlers wired."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                              MessageHandler, filters)

    core = core or BotCore()
    settings = core.settings

    async def _guard(update: "Update") -> bool:
        chat = update.effective_chat
        if not core.is_authorized(chat.id if chat else ""):
            if update.message:
                await update.message.reply_text("Unauthorized.")
            return False
        return True

    async def _reply(update: "Update", text: str) -> None:
        for chunk in _chunks(text):
            await update.message.reply_text(chunk)

    async def on_command(update: "Update", context) -> None:  # noqa: ANN001
        if not await _guard(update):
            return
        cmd = update.message.text.split()[0].lstrip("/").split("@")[0]
        arg = update.message.text[len(update.message.text.split()[0]):].strip()

        def _kb(rows):
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton(lbl, callback_data=data) for lbl, data in row] for row in rows])

        if cmd == "jobs":
            header, jobs = core.jobs_payload()
            if not jobs:
                await _reply(update, header)
                return
            lines = [header] + [f"[{j['id']}] {j['title']} @ {j['company']} ({j['score']}/100)" for j in jobs]
            await update.message.reply_text("\n".join(lines), reply_markup=_kb(job_buttons(jobs)))
            return
        if cmd == "quiz":
            text = core.run_command_raw("spaced_rep", "quiz", {"unit": arg})
            markup = _kb(REVEAL_BUTTON) if core.quiz_active() else None
            await update.message.reply_text(text[:4000], reply_markup=markup)
            return
        if cmd == "cards":
            cands = core.candidates()
            if not cands:
                await _reply(update, "No candidate cards awaiting approval.")
                return
            for c in cands[:15]:
                await update.message.reply_text(
                    f"({c['unit']}) {c['front']} → {c['back']}",
                    reply_markup=_kb(candidate_buttons([c])))
            return
        if cmd == "deadlines":
            pend = core.pending_deadlines()
            if not pend:
                await _reply(update, core.run_command_raw("semester_planner", "due", {}))
                return
            await _reply(update, "Confirm these deadlines I found in your email:")
            for d in pend[:15]:
                await update.message.reply_text(
                    f"{d['title']} ({d['unit'] or 'general'}, {d['type'] or 'task'})",
                    reply_markup=_kb(deadline_buttons([d])))
            return
        if cmd == "deadline":
            toks = arg.split(maxsplit=1)
            if len(toks) < 2:
                await _reply(update, "Usage: /deadline <YYYY-MM-DD> <title>")
                return
            await _reply(update, core.run_command_raw(
                "semester_planner", "deadline_add", {"due": toks[0], "title": toks[1]}))
            return
        if cmd == "events":
            events = core.events(arg)
            if not events:
                await _reply(update, "No matching free events right now.")
                return
            for e in events[:10]:
                icon = "🌐" if e["format"] == "online" else "📍"
                await update.message.reply_text(
                    f"{icon} {e['title']} — {(e['date'] or 'TBA')[:10]}\n{e['url']}",
                    reply_markup=_kb(event_buttons([e])))
            return
        if cmd == "tags":
            toks = arg.split(maxsplit=1)
            action = toks[0] if toks else "list"
            tag = toks[1] if len(toks) > 1 else ""
            await _reply(update, core.run_command_raw("event_scout", "tags",
                                                      {"action": action, "tag": tag}))
            return
        if cmd == "cv":
            sub, _, rest = arg.partition(" ")
            if sub == "update":
                await _reply(update, core.run_command_raw("cv_tailor", "update", {}))
            elif sub == "tailor":
                await _reply(update, core.run_command_raw("cv_tailor", "tailor", {"target": rest.strip()}))
            elif sub == "facts":
                await _reply(update, core.run_command_raw("cv_tailor", "facts", {}))
            else:
                await _reply(update, core.run_command_raw("cv_tailor", "view", {}))
            return
        await _reply(update, core.run_command(cmd, arg))

    async def on_callback(update: "Update", context) -> None:  # noqa: ANN001
        query = update.callback_query
        if not core.is_authorized(query.message.chat.id):
            await query.answer("Unauthorized.")
            return
        await query.answer()
        text = core.handle_callback(query.data)
        markup = None
        if query.data == "q:reveal":
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                 for row in GRADE_BUTTONS])
        elif query.data.startswith("q:grade") and core.quiz_active():
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                 for row in REVEAL_BUTTON])
        await query.message.reply_text(text[:4000], reply_markup=markup)

    async def on_text(update: "Update", context) -> None:  # noqa: ANN001
        if not await _guard(update):
            return
        await _reply(update, core.route_text(update.message.text))

    async def on_voice(update: "Update", context) -> None:  # noqa: ANN001
        if not await _guard(update):
            return
        import tempfile

        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        path = f"{tempfile.gettempdir()}/agentos_voice_{voice.file_id[:8]}.ogg"
        await tg_file.download_to_drive(path)
        transcript = core.transcribe(path)
        if not transcript:
            await _reply(update, "Sorry, I couldn't transcribe that.")
            return
        await _reply(update, f"🎙 “{transcript}”\n\n" + core.route_text(transcript))

    app = Application.builder().token(settings.telegram_bot_token).build()
    known = (["start", "help", "status", "jobs", "approve", "voiceoff", "voiceon", "summarize",
              "quiz", "cards", "deadline", "deadlines", "events", "tags"] + list(COMMAND_MAP.keys()))
    app.add_handler(CommandHandler(known, on_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def run() -> None:
    """Blocking entry point — starts long-polling. Launched by `manage.py telegram` / PM2."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env.")
    log.info("Starting AgentOS Telegram bot…")
    build_application().run_polling(allowed_updates=["message", "callback_query"])


# No SKILL export: this is a standalone process, not a dispatchable skill. Discovery skips it.
