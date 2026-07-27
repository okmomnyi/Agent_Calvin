"""Telegram bot — full remote control of AgentOS (Phase 8).

Single authorized chat id. Commands (/menu for the full, always-current list) — inline
Apply/Skip/Details buttons on the job digest, free text routed through the SAME intent
engine as voice, and voice notes transcribed on the droplet (faster-whisper) and routed
identically — the reliable "call it from my phone" path (Phase 7 phone access).

BotCore holds all the testable logic (authorization, status, callbacks, routing); the
python-telegram-bot handlers at the bottom are thin async wrappers. PTB is imported lazily
so importing this module never requires the library or starts polling.

Phase 40 — "does the bot feel alive": three real transcript bugs, one fix. (1) A correct
command could return NOTHING until the work finished — indistinguishable from a dead bot.
The old fix was `_PROGRESS_PATTERNS`, a hand-maintained regex table matched against RAW
TEXT before routing, and it explicitly skipped every slash command (`if raw.startswith("/")
: return ""`) — so the majority of real usage got no ack at all. (2) A wrong/unrecognized
slash command was WORSE than silent: `CommandHandler(known, on_command)` only registers a
hand-typed `known` list, so anything outside it never reached `on_command` at all — the
`"Unknown command /{cmd}. Try /help."` fallback already written in `run_command()` was
dead code Telegram never let fire. (3) `/start`'s help text was a second hand-maintained
list, independent of both COMMAND_MAP and whatever skills are actually registered.

The fix is ONE mechanism, at the router: `PreparedReply` (`BotCore.prepare()` /
`.prepare_command()`) resolves a message to its (skill, action) via
`registry.route()`/`registry.ack_for()` (kernel/registry.py, Phase 40) and returns EITHER
an ack (the skill's own declared wording, core/skill.py's `ack_for()`) to show before the
work runs, OR a helpful error (a closest-match guess via `core.intent.closest_match()`,
stdlib `difflib` — no fuzzy-matcher existed anywhere in this codebase before this) — never
both, never neither for an ordinary command. `_PROGRESS_PATTERNS`/`progress_line()` are
gone; the catch-all `MessageHandler(filters.COMMAND, on_command)` replaces the hand-typed
`known` list; `/menu` (registry-derived, also calls `setMyCommands`) replaces the old
hand-typed `/start` wall of text.
"""

from __future__ import annotations

import inspect
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.config import get_settings
from core.intent import closest_match
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import ACK_DEFAULT_LATENCY, ACK_DEFAULT_TEXT
from kernel.registry import SkillRegistry

log = get_logger("skills.telegram_bot")

# command -> (skill, action, arg_key). Special commands (status/jobs/approve/voice) handled separately.
COMMAND_MAP: dict[str, tuple[str, str, str | None]] = {
    "ask": ("persona", "answer", "question"),
    # Both routed to the SAME action: /research had no COMMAND_MAP entry at all (the real
    # bug -- "/research Camaro" fell through to /find as a workaround), and every path
    # into this skill always builds the same house-style PDF now, so there is nothing
    # left to route them to differently.
    "find": ("research", "search", "query"),
    "research": ("research", "search", "query"),
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
    "news": ("world_news", "whats_up", "categories"),
    "whatsup": ("world_news", "whats_up", "categories"),
    "markets": ("markets", "snapshot", None),
}

# Slash commands handled with custom logic (an orchestrator call, a live OTP flow, an
# inline-keyboard reply) rather than a plain (skill, action) dispatch — these get NO
# router-level ack (mostly instant/interactive, not the "long silent wait" this phase
# exists to fix) and are never candidates for the "unroutable" error either, since they
# unambiguously exist. "plan" is deliberately here, not resolved via COMMAND_MAP's own
# (shadowed) "plan" entry: run_command() has always special-cased /plan to the
# orchestrator ahead of the COMMAND_MAP lookup, so COMMAND_MAP["plan"] never actually
# fires as a slash command today — a pre-existing quirk this phase doesn't change.
_NO_ACK_COMMANDS = frozenset({
    "start", "help", "status", "plan", "jobs", "quiz", "cards", "deadlines", "deadline",
    "events", "tags", "cv", "resetpassword", "menu",
})

# (skill, action) -> the slash word that reaches it OUTSIDE of COMMAND_MAP (hardcoded
# branches in run_command()). Bridges the same pair for both the ack lookup and /menu's
# reverse "what word types this" display.
_SPECIAL_COMMAND_TARGETS: dict[tuple[str, str], str] = {
    ("job_hunter", "approve"): "approve",
    ("voice", "mute"): "voiceoff",
    ("voice", "unmute"): "voiceon",
    ("email_agent", "digest"): "summarize",
}


def _command_word_for(skill: str, action: str) -> str | None:
    """The slash word that reaches (skill, action), if one exists — COMMAND_MAP first,
    then the hardcoded specials. None means this capability has no slash command at all
    (still reachable by free text/voice, and still shown in /menu, just not as a "/word").

    Some actions have more than one alias (e.g. "find"/"research" both reach
    research.search — "find" predates the Phase 39 fix and is kept only for backward
    compatibility). When a skill-named alias exists, prefer it — it's the discoverable,
    self-documenting one — rather than whichever alias happens to appear first."""
    matches = [cmd for cmd, (s, a, _arg) in COMMAND_MAP.items() if (s, a) == (skill, action)]
    if skill in matches:
        return skill
    if matches:
        return matches[0]
    return _SPECIAL_COMMAND_TARGETS.get((skill, action))


def _resolve_command_skill_action(cmd: str) -> tuple[str, str] | None:
    """(skill, action) for a slash command NOT in `_NO_ACK_COMMANDS` — the mirror image of
    `_command_word_for`, used to look up the ack (or conclude the command is unroutable)
    before it actually runs."""
    if cmd == "approve":
        return ("job_hunter", "approve")
    if cmd in ("voiceoff", "voiceon"):
        return ("voice", "mute" if cmd == "voiceoff" else "unmute")
    if cmd == "summarize":
        return ("email_agent", "digest")
    if cmd in COMMAND_MAP:
        skill, action, _arg_key = COMMAND_MAP[cmd]
        return (skill, action)
    return None


# /menu's grouping (Jobs · Study · Email · Markets · News · Research · Media · System) —
# a skill absent here defaults to "System" rather than needing a hand-edit every time a
# new skill is added.
_MENU_GROUPS: dict[str, str] = {
    "job_hunter": "Jobs", "cv_tailor": "Jobs", "interview_prep": "Jobs", "event_scout": "Jobs",
    "spaced_rep": "Study", "vault": "Study", "code_tutor": "Study",
    "semester_planner": "Study", "lecture_capture": "Study",
    "email_agent": "Email",
    "markets": "Markets",
    "world_news": "News",
    "research": "Research",
    "music": "Media", "youtube": "Media",
}
_DEFAULT_MENU_GROUP = "System"
_MENU_GROUP_ORDER = ["Jobs", "Study", "Email", "Markets", "News", "Research", "Media", "System"]

# Retired (Phase 40): the old /start reply was a second hand-maintained command list,
# independent of both COMMAND_MAP and whatever's actually registered — /menu (built live
# from the registry) is the one source of truth now; this is just a short welcome.
HELP = (
    "👋 I'm AgentOS.\n\n"
    "Send /menu for everything I can do right now — grouped, and always current with "
    "whatever's actually registered.\n\n"
    "Or just type or say what you want naturally; I'll route it. Voice notes work too."
)


@dataclass
class PreparedReply:
    """Phase 40's one router-level ack/error primitive. Exactly one of `ack`/`error` is
    set for an ordinary command — or BOTH are None only for a session continuation
    (mid-quiz answer, a pending trash/send confirmation, ...), which runs its own
    established flow via `finish()` with no pre-emptive ack (see BotCore.prepare()).
    Callers: send `ack` first if set (or `error` INSTEAD, never both), THEN call
    `finish()` exactly once for the real result — this is what makes "one send per
    outcome" (ack, then result/error, never stacked, never duplicated) a fact of the
    call shape rather than something each caller has to remember.
    """

    ack: str | None
    error: str | None
    finish: Callable[[], str]

_MOCK_KEY = "interview_prep.mock"
_QUIZ_KEY = "spaced_rep.session"
_TUTOR_KEY = "code_tutor.session"
# A tutoring/quiz/mock exchange is a conversation, not a mode: 45 minutes of silence
# means it is over. Long enough for a real drill, short enough that a forgotten
# session cannot swallow tomorrow.
_LIVE_SESSION_TTL = 45 * 60
_TRASH_KEY = "email_agent.trash_session"
_SEND_KEY = "email_agent.send_session"
_PW_RESET_KEY = "account_security.password_reset_session"
_PW_RESET_TTL = 600  # 10 minutes: matches the OTP's own expiry (core/auth.py)


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
                 transcribe: Callable[[str], str] | None = None,
                 mailer: Any | None = None) -> None:
        self.settings = get_settings()
        self.mem = memory or get_memory()
        self.registry = registry or SkillRegistry()
        if registry is None:
            self.registry.discover()
        self._transcribe = transcribe
        self._mailer = mailer
        self.started = time.time()

    # ------------------------------------------------------------- auth
    def is_authorized(self, chat_id: int | str) -> bool:
        allowed = self.settings.telegram_chat_id
        return bool(allowed) and str(chat_id) == str(allowed)

    @property
    def mailer(self) -> Any:
        # Injected so tests can never send a real email -- same pattern as email_agent.py's
        # own mailer property.
        if self._mailer is None:
            from core.mailer import ApplicationMailer

            self._mailer = ApplicationMailer()
        return self._mailer

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
        if cmd == "menu":
            return self.menu_text()
        if cmd == "status":
            return self.status_text()
        if cmd == "approve":
            ids = [int(n) for n in arg.replace(",", " ").split() if n.strip().isdigit()]
            return self._dispatch("job_hunter", "approve", {"selection": ids})
        if cmd == "plan":
            try:
                orchestrator = self.registry.orchestrator
                if arg.strip():
                    return orchestrator.run(arg.strip(), channel="telegram").text
                rows = self.mem.list_plans(active_only=True)
                if not rows:
                    return "No active plans. Use /plan <goal> to start one."
                return "Active plans:\n" + "\n".join(
                    f"- {r['id']} [{r['status']}] {r['goal']}" for r in rows)
            except Exception as exc:  # noqa: BLE001
                return f"I couldn't open plans right now: {exc}"
        if cmd in ("voiceoff", "voiceon"):
            return self._dispatch("voice", "mute" if cmd == "voiceoff" else "unmute", {})
        if cmd == "summarize":
            return self.route_text(f"summarize {arg}")
        if cmd in COMMAND_MAP:
            skill, action, arg_key = COMMAND_MAP[cmd]
            payload = {arg_key: arg} if arg_key else {}
            return self._dispatch(skill, action, payload)
        guess = closest_match(cmd, self._known_command_words())
        if guess:
            return f'I couldn\'t place "/{cmd}" — did you mean /{guess}? See /menu for everything I can do.'
        return f'I couldn\'t place "/{cmd}". See /menu for everything I can do.'

    def _session_fresh(self, key: str, ttl: int = 600) -> bool:
        """True only if a stored session exists AND is younger than ttl.

        A STALE session must not swallow an unrelated message: Calvin's "write and send an
        email ..." got eaten by a 10-minute-old trash preview, which replied "expired, start
        again" and consumed it. A stale session is cleared and the message falls through to
        normal routing.
        """
        raw = self.mem.kv_get(key)
        if not raw:
            return False
        try:
            state = json.loads(raw)
            created = float(state.get("created_at", 0) or 0)
        except Exception:  # noqa: BLE001 - malformed session is stale by definition
            self.mem.kv_set(key, "")
            return False
        if not created:
            # Older sessions (quiz, mock, tutor) never recorded a start time. Killing them on
            # sight would break a live drill mid-answer, so stamp them NOW and age from here:
            # a genuine in-progress session survives, a forgotten one still expires.
            state["created_at"] = time.time()
            self.mem.kv_set(key, json.dumps(state))
            return True
        if time.time() - created > ttl:
            self.mem.kv_set(key, "")     # expire it silently; do not intercept
            return False
        return True

    def _try_approval_reply(self, text: str) -> str | None:
        """Handle "3 yes" / "always no 3" / "yes all" against pending actions (Phase 30).

        Returns None when the message isn't an approval reply, so ordinary conversation still
        routes normally. Only consulted when something is actually pending -- otherwise a bare
        "3" in conversation would be read as approving action 3.
        """
        from core.approvals import get_store, parse_approval_reply

        try:
            store = get_store(self.mem)
            pending = store.pending()
            if not pending:
                return None
            parsed = parse_approval_reply(text)
            if not parsed:
                return None
        except Exception:  # noqa: BLE001 - approvals must never break normal messaging
            return None

        if parsed.get("bulk"):
            n = store.resolve_all(approve=parsed["approve"])
            verb = "Approved" if parsed["approve"] else "Denied"
            return f"{verb} all {n} pending action(s)."

        action = store.resolve(parsed["id"], approve=parsed["approve"],
                               always=parsed.get("always", False))
        if action is None:
            # Namespace collision, not a real miss: "approve 6229,6234,6235" from a job
            # digest names JOB ids, a completely different sequence from pending_actions
            # (Phase 30's proposal queue, used by proactive.py). Returning an error here
            # dead-ended the message before it ever reached job_hunter.approve(), which
            # would have resolved it correctly. Returning None instead lets route_text()
            # fall through to the normal router -- and if the id genuinely isn't valid
            # ANYWHERE, whichever skill actually owns that id space reports its own honest
            # "unknown id" rather than this one guessing on the wrong table.
            return None
        verb = "✅ Approved" if parsed["approve"] else "🚫 Denied"
        note = ""
        if parsed.get("always"):
            if action.tier == "high" and parsed["approve"]:
                # Told him rather than silently ignoring it: a safety rule he thinks he
                # turned off, but didn't, is worse than one that says no out loud.
                note = ("\n(Not remembered — this acts in your name, so it will keep asking. "
                        "§0 P3.)")
            else:
                note = "\n(Remembered — I won't ask about this pattern again.)"
        return f"{verb}: {action.description}{note}"

    # ------------------------------------------------------------- ack / menu / errors (Phase 40)
    def _known_command_words(self) -> list[str]:
        """Every real slash word — COMMAND_MAP plus the hardcoded specials — the pool a
        "did you mean...?" guess is drawn from."""
        return sorted(set(COMMAND_MAP.keys()) | _NO_ACK_COMMANDS | {
            "approve", "voiceoff", "voiceon", "summarize",
        })

    def _is_continuation(self, text: str) -> bool:
        """True if this free-text message would be swallowed by an active session/reply
        flow instead of ordinary routing — read-only checks only (mirrors route_text()'s
        own precedence exactly), so this is always safe to call before route_text() runs
        without double-triggering anything stateful."""
        try:
            from core.orchestrator import is_plan_reply

            if is_plan_reply(text) and self.mem.list_plans(active_only=True):
                return True
        except Exception:  # noqa: BLE001 - an unavailable planner must not block routing
            pass
        try:
            from core.approvals import get_store, parse_approval_reply

            store = get_store(self.mem)
            if store.pending() and parse_approval_reply(text):
                return True
        except Exception:  # noqa: BLE001 - an unavailable approval store degrades to "no"
            pass
        if self._session_fresh(_SEND_KEY) or self._session_fresh(_TRASH_KEY):
            return True
        if self._session_fresh(_MOCK_KEY, ttl=_LIVE_SESSION_TTL):
            return True
        if self._session_fresh(_QUIZ_KEY, ttl=_LIVE_SESSION_TTL):
            return True
        if self._session_fresh(_TUTOR_KEY, ttl=_LIVE_SESSION_TTL):
            return True
        return False

    @staticmethod
    def _unroutable_text(shown: str, guess: str | None) -> str:
        shown = shown if len(shown) <= 60 else shown[:57] + "…"
        if guess:
            return f'I couldn\'t place "{shown}" — did you mean /{guess}? See /menu for everything I can do.'
        return f'I couldn\'t place "{shown}". See /menu for everything I can do.'

    def prepare(self, text: str) -> PreparedReply:
        """The router-level ack/error mechanism for FREE TEXT. Resolves once via
        `registry.route()`, looks up the accepting skill's own ack via
        `registry.ack_for()`, and returns a PreparedReply whose `finish()` re-runs
        `route_text()` (unchanged) to get the real result — never both an ack and an
        error, and a continuation gets neither (its own flow already replies).
        """
        if text.startswith("/") or self._is_continuation(text):
            return PreparedReply(None, None, lambda: self.route_text(text))
        intent = self.registry.route(text, use_llm=True)
        ack = self.registry.ack_for(intent)
        if ack is None:
            guess = closest_match(text, self._known_command_words())
            error = self._unroutable_text(text, guess)
            return PreparedReply(None, error, lambda: error)
        return PreparedReply(ack[0], None, lambda: self.route_text(text))

    def prepare_command(self, cmd: str, arg: str = "") -> PreparedReply:
        """The router-level ack/error mechanism for SLASH COMMANDS. A command with custom
        (interactive-keyboard / instant / orchestrator) handling in `_NO_ACK_COMMANDS`
        gets no ack — `on_command`'s own existing branches run it, `finish()` here is
        never actually called for those. Anything else resolves via
        `_resolve_command_skill_action()`; no resolution -> the closest-match error.
        """
        cmd = cmd.lstrip("/").lower()
        if cmd in _NO_ACK_COMMANDS:
            return PreparedReply(None, None, lambda: self.run_command(cmd, arg))
        resolved = _resolve_command_skill_action(cmd)
        if resolved is None:
            guess = closest_match(cmd, self._known_command_words())
            error = (f'I couldn\'t place "/{cmd}" — did you mean /{guess}? See /menu for '
                    "everything I can do." if guess else
                    f'I couldn\'t place "/{cmd}". See /menu for everything I can do.')
            return PreparedReply(None, error, lambda: error)
        skill_name, action = resolved
        skill = self.registry.get(skill_name)
        ack_text, _latency = skill.ack_for(action) if skill is not None else (
            ACK_DEFAULT_TEXT, ACK_DEFAULT_LATENCY)
        return PreparedReply(ack_text, None, lambda: self.run_command(cmd, arg))

    def menu_entries(self) -> list[dict[str, str]]:
        """[{group, command, description}], derived ENTIRELY from the live registry
        (`registry.manifest()`) — never hand-maintained. A skill with no wired slash
        command still appears exactly once (so a newly-registered skill is always
        visible with zero hand-edits here), shown by its skill+action pair rather than a
        "/word" it doesn't have."""
        seen_skills: set[str] = set()
        entries: list[dict[str, str]] = []
        for item in self.registry.manifest():
            skill, action, doc = item["skill"], item["action"], item["doc"]
            cmd_word = _command_word_for(skill, action)
            if cmd_word is None and skill in seen_skills:
                continue
            seen_skills.add(skill)
            group = _MENU_GROUPS.get(skill, _DEFAULT_MENU_GROUP)
            command = f"/{cmd_word}" if cmd_word else skill
            entries.append({"group": group, "command": command,
                            "description": doc or f"{skill} {action}"})
        return entries

    def menu_text(self) -> str:
        """The /menu message — grouped, scannable, always current."""
        entries = self.menu_entries()
        by_group: dict[str, list[dict[str, str]]] = {}
        for e in entries:
            by_group.setdefault(e["group"], []).append(e)
        order = list(_MENU_GROUP_ORDER) + sorted(g for g in by_group if g not in _MENU_GROUP_ORDER)
        lines = ["🧭 What I can do (always current):"]
        for group in order:
            items = by_group.get(group)
            if not items:
                continue
            lines.append(f"\n{group}")
            for e in sorted(items, key=lambda x: x["command"]):
                lines.append(f"{e['command']} — {e['description']}")
        lines.append("\nOr just say it naturally — I'll route it.")
        return "\n".join(lines)

    def telegram_commands(self) -> list[tuple[str, str]]:
        """(command, description) pairs for Telegram's `setMyCommands` — ONLY entries
        with a real, typeable "/word" (Telegram rejects a command name containing a
        space), deduplicated, description capped at Telegram's own 256-char limit."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for e in self.menu_entries():
            if not e["command"].startswith("/"):
                continue
            word = e["command"][1:]
            if word in seen:
                continue
            seen.add(word)
            out.append((word, (e["description"] or word)[:256]))
        return out

    def _consume(self, key: str, skill: str, action: str, payload: dict) -> str:
        """Run one continuation, then end that session.

        Cleared BEFORE dispatching, not after: if the skill raises, the session must still be
        gone. A crash that leaves the mode latched is exactly how a two-day tutor session
        happened in the first place.
        """
        self.mem.kv_set(key, "")
        out = self._dispatch(skill, action, payload)
        return out + "\n\n(Session closed — say it again any time to pick it back up.)"

    # ------------------------------------------------------------- password reset (Phase 36)
    # Being the one authorized Telegram chat is not proof enough to hand out a fresh account
    # password over it (§0 P3: this is the master credential, so it gets its own second
    # factor). The code only ever reaches config.yaml's auth.password_reset_email inbox --
    # never Telegram itself -- and the new password is shown here exactly once, then the PTB
    # layer deletes that one message a few minutes later (see build_application's on_text).
    def _password_reset_target(self) -> str:
        try:
            return self.settings.get("auth", "password_reset_email", default="") or ""
        except AttributeError:  # a bare test-fixture Settings has no .get() -- fail closed
            return ""

    def start_password_reset(self) -> str:
        """/resetpassword — mints and emails an OTP, then waits for the reply."""
        from core.auth import get_store

        store = get_store(self.mem)
        locked, retry_after = store.lockout_status("password_reset_otp", "")
        if locked:
            return f"Too many recent attempts — try again in about {int(retry_after)}s."
        target = self._password_reset_target()
        if not target:
            return ("No password-reset email is configured "
                    "(set auth.password_reset_email in config.yaml).")
        code = store.request_password_reset_otp()
        try:
            self.mailer.send_email(
                to=target, subject="AgentOS password reset code",
                body=(f"Your AgentOS password reset code is {code}\n\n"
                      "It expires in 10 minutes and works once. Reply with it in Telegram "
                      "to finish resetting your password.\n\n"
                      "Didn't request this? Ignore it — nothing changes until the code is used."))
        except Exception as exc:  # noqa: BLE001 - never claim a code was sent if it wasn't
            return f"Couldn't send the verification email: {exc}"
        self.mem.kv_set(_PW_RESET_KEY, json.dumps({"created_at": time.time()}))
        return (f"📧 Sent a verification code to {target}. Reply with the 6-digit code within "
                "10 minutes to reset your password, or 'cancel' to stop.")

    def awaiting_password_reset_code(self) -> bool:
        return self._session_fresh(_PW_RESET_KEY, ttl=_PW_RESET_TTL)

    def continue_password_reset(self, text: str) -> tuple[str, bool]:
        """Second step: verify the OTP and, on success, set + reveal a fresh password.

        Returns (reply_text, self_destruct) — self_destruct tells the caller to delete its
        own reply a few minutes later. The OTP itself is never echoed back here; only a
        freshly-generated PASSWORD is, and only after the code has actually verified.
        """
        from core.auth import get_store

        store = get_store(self.mem)
        answer = (text or "").strip()
        if answer.lower() in {"cancel", "stop", "no"}:
            self.mem.kv_set(_PW_RESET_KEY, "")
            return "Cancelled — your password wasn't changed.", False

        locked, retry_after = store.lockout_status("password_reset_otp", "")
        if locked:
            self.mem.kv_set(_PW_RESET_KEY, "")
            return (f"Too many wrong codes — locked for about {int(retry_after)}s. "
                    "Run /resetpassword again once that passes."), False

        ok = store.verify_password_reset_otp(answer)
        store.record_attempt("password_reset_otp", ok, ip="")
        if not ok:
            return ("That code didn't match (or it expired). Reply with the code again, "
                    "or 'cancel'."), False

        self.mem.kv_set(_PW_RESET_KEY, "")
        new_password = secrets.token_urlsafe(18)
        store.set_password(new_password)
        revoked = store.revoke_all_sessions(keep_devices=True)
        note = f" ({revoked} other signed-in session(s) signed out.)" if revoked else ""
        return (f"🔑 Password reset. New password:\n\n{new_password}\n\n"
                f"Save it now — this message deletes itself in 3 minutes.{note}"), True

    def route_text(self, text: str) -> str:
        """Free text: continue an active (FRESH) session if one is running, else route via intent."""
        if not text.startswith("/"):
            try:
                plan_reply = self.registry.orchestrator.handle_reply(text)
                if plan_reply is not None:
                    return plan_reply.text
            except Exception:  # noqa: BLE001 - normal chat survives an unavailable plan store
                pass
            approval = self._try_approval_reply(text)
            if approval is not None:
                return approval
            if self._session_fresh(_SEND_KEY):   # "confirm send" / "cancel" after a compose preview
                return self._dispatch("email_agent", "continue_send", {"text": text})
            if self._session_fresh(_TRASH_KEY):  # "confirm trash" / "cancel" / a follow-up filter
                return self._dispatch("email_agent", "continue_trash", {"text": text})
            # Every conversational session is now age-checked, not just trash/send. A
            # `/tutor` session from two days ago had been intercepting EVERY free-text
            # message since -- his "write and send an email" got an smtplib tutorial and
            # "create a playlist" got C++ classes, because route_text consults these before
            # the router and they had no expiry at all. A session that cannot age is a
            # session that hijacks the assistant forever.
            # ONE-SHOT: consume the continuation, then end the session. The next message
            # routes fresh, so a forgotten drill can never reinterpret tomorrow's request.
            if self._session_fresh(_MOCK_KEY, ttl=_LIVE_SESSION_TTL):
                return self._consume(_MOCK_KEY, "interview_prep", "mock_answer",
                                     {"answer": text})
            if self._session_fresh(_QUIZ_KEY, ttl=_LIVE_SESSION_TTL):  # quiz answer -> judged
                return self._consume(_QUIZ_KEY, "spaced_rep", "quiz_answer", {"answer": text})
            if self._session_fresh(_TUTOR_KEY, ttl=_LIVE_SESSION_TTL):  # drill/socratic/lab
                return self._consume(_TUTOR_KEY, "code_tutor", "continue", {"text": text})
        handler = self.registry.handle_command
        if "channel" in inspect.signature(handler).parameters:
            _intent, result = handler(text, channel="telegram")
        else:
            _intent, result = handler(text)
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
        # jobs_by_status() sorts by score WITHIN one status, but concatenating two already-
        # sorted lists doesn't sort them together — every "notified" job (whatever its score)
        # used to land ahead of every "drafted" job, so an 85-scored draft could sit behind a
        # 60-scored notified job. Fetch generously, re-sort the combined set by score, THEN cap.
        rows = self.mem.jobs_by_status("notified", limit=20) + self.mem.jobs_by_status("drafted", limit=20)
        rows = sorted(rows, key=lambda r: r["score"] or 0, reverse=True)[:10]
        jobs = [{"id": r["id"], "title": r["title"], "company": r["company"],
                 "score": r["score"], "category": r["category"],
                 "apply_kind": r["apply_kind"], "apply_target": r["apply_target"]} for r in rows]
        if not jobs:
            return "No job matches awaiting approval. Run a hunt or check back after the next scan.", []
        # Say how many are actually waiting. The list is capped at 20 for a readable message,
        # but reporting only the capped number reads as "that's all there is" -- Calvin had 83
        # drafted jobs and was shown 5, with nothing to suggest the other 78 existed. A silent
        # truncation on the approval path is how work quietly never gets done.
        try:
            total = sum(self.mem.count_jobs_by_status(s) for s in ("notified", "drafted"))
        except Exception:  # noqa: BLE001 - a count must never break the listing
            total = len(jobs)
        header = f"💼 {len(jobs)} of {total} job(s) awaiting your call:" if total > len(jobs) \
            else f"💼 {len(jobs)} job(s) awaiting your call:"
        if total > len(jobs):
            header += f"\n(showing the {len(jobs)} highest — {total - len(jobs)} more queued)"
        return header, jobs

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
        try:
            current = self.mem.current_plan()
            plan_line = (f"Current plan: {current['id']} [{current['status']}]\n"
                         if current else "Current plan: none\n")
        except Exception:  # noqa: BLE001
            plan_line = "Current plan: unavailable\n"
        return (f"🟢 AgentOS status\n"
                f"Skills online: {len(self.registry.skills)}\n"
                f"Jobs found today: {jobs_today} (awaiting approval: {awaiting})\n"
                f"Applications tracked: {apps}\n"
                f"Emails processed today: {emails_today}\n"
                f"{plan_line}"
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


async def _delete_later(bot: Any, chat_id: int, message_id: int, delay: float) -> None:
    """Best-effort self-destruct for the one message that ever shows the reset password.

    In-process only (an asyncio task, not a persisted job) -- if the bot restarts within the
    window the delete simply never fires. That's an accepted gap: the OTP gate and the
    argon2-hashed password are the actual security boundary, this only shrinks how long the
    plaintext sits visible in the chat.
    """
    import asyncio

    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001 - already deleted/too old/etc. is fine
        log.debug("could not auto-delete the password-reset message", exc_info=True)


def build_application(core: BotCore | None = None):
    """Build the python-telegram-bot Application with all handlers wired."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

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

        # Phase 40: the router-level ack/error, BEFORE any special-case handling below —
        # exactly one of ack/error fires, ack always before the eventual result, and
        # _NO_ACK_COMMANDS (the commands with their OWN custom handling right below) get
        # neither, since `on_command`'s own branches — not `prepared.finish()` — run them.
        prepared = core.prepare_command(cmd, arg)
        if prepared.error:
            await _reply(update, prepared.error)
            return
        if prepared.ack:
            await update.message.reply_text(prepared.ack)
        if cmd == "menu":
            await _reply(update, core.menu_text())
            try:  # keep the native "/" popup in sync every time /menu is asked for
                from telegram import BotCommand

                await context.bot.set_my_commands(
                    [BotCommand(word, desc) for word, desc in core.telegram_commands()])
            except Exception:  # noqa: BLE001 - the menu MESSAGE must still land either way
                log.warning("setMyCommands failed", exc_info=True)
            return

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
        if cmd == "resetpassword":
            await _reply(update, core.start_password_reset())
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
        text = update.message.text
        # Checked before anything else: a pending password-reset code must never be
        # swallowed by an unrelated session/router match, and its reply needs to self-destruct,
        # which the generic _reply() helper below has no way to signal.
        if core.awaiting_password_reset_code():
            reply_text, self_destruct = core.continue_password_reset(text)
            msg = await update.message.reply_text(reply_text)
            if self_destruct:
                context.application.create_task(
                    _delete_later(context.bot, msg.chat_id, msg.message_id, delay=180))
            return
        # Say what is being started BEFORE doing it (Phase 40's one router-level ack/error
        # mechanism — see BotCore.prepare()). Calvin: "when i tell the bot to clear emails
        # i need to see clearing emails in progress ... when i say create a playlist i
        # need to se a creating playlist feedback". A long task with no acknowledgement is
        # indistinguishable from a dead bot, and he had no way to tell which he had.
        prepared = core.prepare(text)
        if prepared.error:
            await _reply(update, prepared.error)
            return
        if prepared.ack:
            await update.message.reply_text(prepared.ack)
        await _reply(update, prepared.finish())

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

    async def _post_init(application) -> None:  # noqa: ANN001
        """Populate the native "/" popup at startup so it's current even before anyone
        ever asks for /menu."""
        try:
            from telegram import BotCommand

            await application.bot.set_my_commands(
                [BotCommand(word, desc) for word, desc in core.telegram_commands()])
        except Exception:  # noqa: BLE001 - the bot must still come up if this one call fails
            log.warning("setMyCommands failed at startup", exc_info=True)

    app = Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    # Phase 40: a catch-all for EVERY slash command, known or not — replaces the old
    # hand-typed `known` list, which silently swallowed anything outside it (PTB never
    # even invoked on_command for an unregistered command, so the "Unknown command"
    # fallback already written in run_command() could never actually fire). on_command's
    # own prepare_command() now decides ack vs. the closest-match error for whatever
    # comes through.
    app.add_handler(MessageHandler(filters.COMMAND, on_command))
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
