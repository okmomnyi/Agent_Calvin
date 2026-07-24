"""Natural-language intent router for AgentOS.

Routes a spoken or typed command to a (skill, action, args) triple in two stages:
first a fast, offline keyword/regex match; then, only if that is inconclusive, an
LLM single-label classify() fallback. Keeping keywords first means the common cases
work with zero API latency and the router stays testable without network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger

log = get_logger("core.intent")


@dataclass
class Intent:
    """A resolved intent: which skill/action to run, plus extracted arguments."""

    name: str                       # canonical intent id (see INTENTS)
    skill: str                      # target skill name
    action: str                     # action within the skill
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    via: str = "keyword"            # "keyword" | "llm"


# Canonical intent registry: name -> (skill, action). Mirrors the Phase 1 spec list.
INTENTS: dict[str, tuple[str, str]] = {
    "research":        ("research", "search"),
    "current_time":    ("chat", "time_status"),
    "prep_pack":       ("interview_prep", "prep"),
    "mock_interview":  ("interview_prep", "mock"),
    "check_email":     ("email_agent", "check"),
    "email_search":    ("email_agent", "search"),
    "compose_email":   ("email_agent", "compose"),
    "summarize_inbox": ("email_agent", "digest"),
    "trash_email":     ("email_agent", "trash"),
    "restore_email":   ("email_agent", "restore"),
    "find_jobs":       ("job_hunter", "hunt"),
    "find_events":     ("event_scout", "find"),
    "event_interested": ("event_scout", "interested"),
    "event_skip":       ("event_scout", "skip"),
    "job_status":      ("job_hunter", "status"),
    # These three named skills that have never existed ("router", "approvals", "code_review"),
    # so every one of them matched a keyword rule at confidence 0.9, skipped the catalogue
    # router that would have repaired them, and dead-ended in dispatch_intent's "isn't wired
    # up yet" branch. `approve 1, 3 and 5` is the exact phrasing the approval digest invites,
    # and it was dead on /api/command and /ws/voice -- approvals only ever resolved on Telegram.
    "approve":         ("job_hunter", "approve"),
    "review_code":     ("code_tutor", "review"),
    "answer_form":     ("form_assist", "answer"),
    "tailor_cv":       ("cv_tailor", "tailor"),
    "refine_cv":       ("cv_tailor", "refine"),
    "change_voice":    ("voice", "set_voice"),
    "speak_rate":      ("voice", "set_rate"),
    "remember":        ("persona", "remember"),
    "ask_notes":       ("vault", "ask"),
    "quiz":            ("spaced_rep", "quiz"),
    "tutor":           ("code_tutor", "start"),
    "whats_due":       ("semester_planner", "due"),
    "plan_week":       ("semester_planner", "plan"),
    "cram":            ("semester_planner", "cram"),
    "handoff":         ("session", "handoff"),
    "session_status":  ("session", "status"),
    "approvals_list":  ("session", "approvals"),
    "open_app":        ("desktop", "open"),
    "close_app":       ("desktop", "close"),
    "focus_app":       ("desktop", "focus"),
    "list_apps":       ("desktop", "apps"),
    "music_start":     ("music", "start_session"),
    "music_stop":      ("music", "stop_session"),
    "music_status":    ("music", "session_status"),
    "music_budget":    ("music", "budget"),
    "music_playlist":  ("music", "playlist"),
    "music_pl_remove": ("music", "playlist_remove"),
    "weather":         ("weather", "current"),
    "youtube_play":    ("youtube", "play"),
    "contacts_find":   ("contacts", "find"),
    "phone_call":      ("phone", "call"),
    "phone_answer":    ("phone", "answer"),
    "phone_hangup":    ("phone", "hangup"),
    "chit_chat":       ("chat", "reply"),
}

# Ordered keyword rules. First match wins; capture groups feed args via `arg_key`.
# Each rule: (intent_name, compiled_regex, arg_key_or_None)
_RULES: list[tuple[str, re.Pattern[str], str | None]] = [
    ("current_time", re.compile(
        r"^(?:what(?:'?s| is) (?:the )?(?:time|date)(?: right now| today)?|"
        r"what time is it(?: right now)?|current (?:time|date)|what day is it)\??$", re.I), None),
    # Explicit cross-device handoff (Phase 19) — every group but the channel is non-capturing,
    # because _first_group() returns the first capturing group as the arg.
    ("handoff", re.compile(
        r"\b(?:continu(?:e|ing)|pick(?:ing)?\s+up|carry(?:ing)?\s+on|resum(?:e|ing))\b"
        r".{0,20}?\b(?:from|on)\s+(?:my\s+|the\s+)?"
        r"(?P<t>phone|telegram|laptop|desktop|dashboard|browser|voice|where i left off)",
        re.I), "channel"),
    ("session_status", re.compile(
        r"\b(?:session status|what(?:'?s| is) my session|where was i)\b", re.I), None),
    ("approvals_list", re.compile(
        r"\b(?:approvals|what(?:'?s| is) waiting(?: on me)?|what needs my approval)\b", re.I), None),
    # Phase 36. Both trigger words are unclaimed elsewhere in this table, so these are safe
    # ahead of the generic desktop open_app/close_app rules and don't need the "most specific
    # wins" care those need. The full utterance is captured (not just a sub-phrase) because
    # weather.py's extract_override() and youtube.py's clean_query() each do their own
    # parsing/filter-stripping on the whole text — duplicating that here would be a second
    # normalizer, which is exactly what this router's docstring warns against.
    ("weather", re.compile(r"(?P<t>.*\bweather\b.*)", re.I), "text"),
    ("youtube_play", re.compile(r"(?P<t>.*\byou\s*tube\b.*)", re.I), "query"),
    ("contacts_find", re.compile(r"\bfind contact\s+(?P<t>.+)", re.I), "name"),
    # Answer/hangup checked BEFORE the broad "call X" rule below — "answer the call" ends in
    # the bare word "call" with nothing after it to capture as a name, so it wouldn't collide
    # in practice, but "answer the call now" would (the trailing word becomes a bogus name).
    # Ordering makes that impossible rather than relying on phrasing luck.
    ("phone_answer", re.compile(
        r"\b(?:answer|pick up)\s+(?:the\s+)?(?:phone|call)\b|\banswer\s+it\b", re.I), None),
    ("phone_hangup", re.compile(
        r"\bhang\s?up\b|\bend\s+(?:the\s+)?call\b", re.I), None),
    ("phone_call", re.compile(r"\bcall\s+(?P<t>.+)", re.I), "name"),
    ("change_voice", re.compile(r"\bchange voice to (?P<v>[\w-]+)", re.I), "voice"),
    ("change_voice", re.compile(r"\b(?:switch|set) (?:the )?voice (?:to )?(?P<v>[\w-]+)", re.I), "voice"),
    ("speak_rate", re.compile(r"\bspeak (?P<t>slower|faster|quicker)\b", re.I), "direction"),
    ("remember", re.compile(r"\b(?:remember|note) that\b[:,]?\s*(?P<t>.+)", re.I), "instruction"),
    ("remember", re.compile(r"\bremember\b[:,]?\s*(?P<t>.+)", re.I), "instruction"),
    ("approve", re.compile(r"\b(?:approve|apply to|send)(?: number| numbers)?\s+(?P<t>[\d ,and]+)$", re.I), "selection"),
    ("tailor_cv", re.compile(r"\btailor (?:my )?cv\b(?: for (?P<t>.+))?", re.I), "target"),
    ("refine_cv", re.compile(
        r"\b(?:refine|improve|polish|optimise|optimize|rewrite|update)\s+"
        r"(?:my\s+)?(?:cv|resume|résumé)\b(?:\s+for\s+(?P<t>.+))?", re.I), "target"),
    ("ask_notes", re.compile(r"\bask (?:my )?notes\b[:,]?\s*(?P<t>.+)", re.I), "question"),
    ("quiz", re.compile(r"\bquiz me\b(?: on (?P<t>.+))?", re.I), "unit"),
    ("tutor", re.compile(
        r"\btutor(?:\s+me)?(?:\s+mode)?\b(?:\s+(?:on|about|me\s+on))?"
        r"[:,]?\s*(?P<t>.*)", re.I), "topic"),
    ("cram", re.compile(r"\bcram\b\s*(?P<t>.+)?", re.I), "unit"),
    ("mock_interview", re.compile(r"\bmock (?:interview|me)\b(?: for)?\s*(?P<t>.+)?", re.I), "company"),
    ("prep_pack", re.compile(r"\b(?:prep|prepare)(?: me)?(?: for)?\s+(?P<t>.+)", re.I), "company"),
    ("plan_week", re.compile(r"\bplan (?:my )?week\b", re.I), None),
    ("whats_due", re.compile(r"\bwhat(?:'?s| is)? due\b|\bmy deadlines\b", re.I), None),
    ("event_interested", re.compile(
        r"\b(?:interested in|add|save)\s+(?:the\s+)?event\s+#?\s*(?P<t>\d+)\b", re.I),
        "event_id"),
    ("event_skip", re.compile(
        r"\b(?:skip|ignore)\s+(?:the\s+)?event\s+#?\s*(?P<t>\d+)\b", re.I),
        "event_id"),
    ("find_events", re.compile(r"\b(?:any )?(?:free )?events?\b|\bany ctfs?\b|\bmeetups?\b", re.I), None),
    ("job_status", re.compile(r"\bjob status\b|\bapplication status\b|\bmy applications\b", re.I), None),
    ("find_jobs", re.compile(r"\b(?:any )?(?:new )?jobs?\b|\bfind (?:me )?(?:a )?jobs?\b|\bfind work\b", re.I), None),
    ("review_code", re.compile(r"\breview (?:my )?(?:code|diff|last diff)\b", re.I), None),
    ("answer_form", re.compile(r"\b(?:answer|help me (?:with|answer)) (?:this )?form\b", re.I), None),
    ("summarize_inbox", re.compile(r"\bsummari[sz]e (?:my )?inbox\b", re.I), None),
    ("restore_email", re.compile(r"\b(?:undo|restore) (?:the )?(?:last )?(?:email )?trash\b", re.I), None),
    # Robust to real phrasing. The old rule needed "emails" immediately after the verb, so
    # "delete all THE emails related to okx", "delete all LINKEDIN emails", and "CLEAR ..."
    # (clear wasn't even a verb) all missed and got guessed as 'tutor'. Now: any delete-ish
    # verb + the word email(s) anywhere -> capture the whole span as the query. email_agent
    # strips the filler and previews; nothing is deleted without a second confirmation.
    # Playlists. Ahead of trash_email, whose catch-all verb list includes "remove" --
    # "remove X from my playlist" routed to EMAIL TRASH before this existed.
    ("music_pl_remove", re.compile(
        r"\b(?:remove|delete|drop|take)\s+(?P<t>.+?)\s+"
        r"(?:from|out\s+of|off)\s+(?:the\s+|my\s+|that\s+)?(?:\w+\s+)?playlists?\b",
        re.I), "track"),
    ("music_pl_remove", re.compile(
        r"\bremove\s+from\s+(?:the\s+|my\s+)?playlists?\b[:,]?\s*(?P<t>.*)", re.I),
        "track"),
    # Two word orders, both seen in practice: "create a playlist FOR late night coding" and
    # "create a late night coding PLAYLIST". `playlist\b` (singular-only) also silently missed
    # "playlists" — a plural that reads perfectly naturally ("create me a ... playlists") --
    # since \b requires a boundary immediately after "playlist", which the trailing "s" denies.
    # Both patterns now accept "playlist" or "playlists".
    ("music_playlist", re.compile(
        r"\b(?:create|make|build|generate|put\s+together)\s+(?:me\s+)?(?:a|an|the)?\s*"
        r"playlists?\b(?:\s+(?:for|about|of|called|named)\s+)?(?P<t>.*)", re.I), "theme"),
    ("music_playlist", re.compile(
        r"\b(?:create|make|build|generate|put\s+together)\s+(?:me\s+)?(?:a|an|the)?\s*"
        r"(?P<t>.+?)\s+playlists?\b", re.I), "theme"),
    # Read-only listing, ahead of trash_email's catch-all verbs so "list/show/find emails
    # from X" never gets a mutating action anywhere near it. There was no dedicated action
    # for this at all before — "list all emails from LinkedIn" fell through to the catalogue
    # router, which had nothing better than `cleanup` to offer and ran a real inbox pass
    # instead of answering what was actually asked.
    ("email_search", re.compile(
        r"\b(?:list|show|find|search)\s+(?:all\s+)?(?:my\s+)?emails?\s+"
        r"(?:from|by|about|regarding)\s+(?P<t>.+)", re.I), "sender"),
    ("trash_email", re.compile(
        r"\b(?:delete|trash|remove|clear|clean\s+(?:up|out)|get\s+rid\s+of)\b"
        r"(?P<t>.*\bemails?\b.*)", re.I), "query"),
    ("trash_email", re.compile(
        r"\b(?:delete|trash|remove|clear)\b(?!.*\b(?:playlist|track|song|music)\b)"
        r"(?P<t>.*)", re.I), "query"),
    # Compose a NEW email (distinct from draft, which replies to an existing thread). The whole
    # "to X saying Y" span is captured and parsed inside compose(); it only ever previews, then
    # waits for 'confirm send'. Placed before check_email so "send an email to ..." doesn't read
    # as "check email".
    ("compose_email", re.compile(
        r"\b(?:write|send|compose)\s+(?:an?\s+)?(?:email|mail|message)\b(?P<t>.*)", re.I),
     "instruction"),
    ("check_email", re.compile(r"\bcheck (?:my )?(?:email|inbox|mail)\b|\bany (?:new )?email\b", re.I), None),
    # Desktop app control (Phase 23) — laptop-side, voice only. Deliberately late in the list
    # and anchored to the end of the utterance: "start"/"open" are common verbs elsewhere
    # ("start a mock interview"), so these must never win ahead of a real skill. An app we
    # don't know still lands here and gets an honest "I don't have that set up".
    # Music session (Phase 27). Ahead of the desktop open/close rules, whose generic verbs
    # ("close ...", "start ...") would otherwise swallow "stop the music".
    ("music_stop", re.compile(
        r"\b(?:stop|kill|end|pause)\s+(?:the\s+)?music\b"
        r"|\bstop\s+(?:the\s+)?(?:music\s+)?session\b|\bmusic\s+off\b", re.I), None),
    ("music_budget", re.compile(
        r"\b(?:music|listening)\s+(?:budget|minutes|hours)\b"
        r"|\bhow\s+much\s+(?:music|have i listened)\b", re.I), None),
    ("music_status", re.compile(
        r"\b(?:music|listening)\s+session\s+status\b|\bwhat(?:'?s| is)\s+playing\b"
        r"|\bis\s+(?:the\s+)?music\s+(?:still\s+)?(?:on|running|playing)\b", re.I), None),
    ("music_start", re.compile(
        r"\b(?:start|play|put\s+on)\s+(?:some\s+|the\s+)?music\b"
        r"|\bstart\s+(?:a\s+|the\s+)?(?:music\s+)?session\b"
        r"|\bkeep\s+(?:the\s+)?music\s+(?:going|playing)\b", re.I), None),
    ("list_apps", re.compile(r"\b(?:what|which) apps\b|\blist apps\b", re.I), None),
    ("open_app", re.compile(
        r"\b(?:open|launch|fire up)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    ("close_app", re.compile(
        r"\b(?:close|quit|shut down)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    ("focus_app", re.compile(
        r"\b(?:focus|switch to|bring up)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    # No generic "summarize X" rule: there is no skill that summarizes an arbitrary thing, and
    # the old one claimed a target ("router.summarize") that does not exist -- so it swallowed
    # the phrase at 0.9 and answered nothing. Specific summaries keep their own rules above
    # (summarize_inbox); everything else falls to the catalogue router, which can pick vault.ask
    # or research.search from what is actually being summarized.
    ("research", re.compile(r"\b(?:search|research|look up|find out)(?: for| about)?\s+(?P<t>.+)", re.I), "query"),
]

# Labels offered to the LLM fallback classifier (only when keywords miss).
_LLM_LABELS = [
    "research", "check_email", "summarize_inbox", "find_jobs", "find_events",
    "job_status", "review_code", "answer_form", "tailor_cv", "remember",
    "ask_notes", "quiz", "chit_chat",
]


# Actions that exist for the machinery, not for Calvin -- passive logging, mid-session
# continuations, callbacks. Routing a sentence to one of these would be worse than missing.
_INTERNAL_ACTIONS = {
    "log_signal", "drill_check", "quiz_answer", "mock_answer", "session_tick",
    "confirm_deadline", "reject_deadline", "score_one", "run", "tick",
}

# The single argument name each skill.action expects, so a routed value lands in the right
# keyword rather than always as `text`. Anything absent falls back to `text`.
_ARG_NAMES = {
    ("music", "playlist"): "theme",
    ("music", "playlist_remove"): "track",
    ("music", "auto_queue"): "cue",
    ("music", "dj"): "cue",
    ("music", "start_session"): "cue",
    ("cv_tailor", "tailor"): "target",
    ("cv_tailor", "refine"): "target",
    ("research", "search"): "query",
    ("vault", "ask"): "question",
    ("interview_prep", "prep"): "company",
    ("interview_prep", "mock"): "company",
    ("code_tutor", "start"): "topic",
    ("code_tutor", "explain"): "topic",
    ("code_tutor", "review"): "code",
    ("spaced_rep", "quiz"): "unit",
    ("semester_planner", "cram"): "unit",
    ("persona", "remember"): "instruction",
    ("email_agent", "compose"): "instruction",
    ("email_agent", "trash"): "query",
    ("desktop", "open"): "app",
    ("desktop", "close"): "app",
    ("proactive", "forget"): "pattern",
    ("youtube", "play"): "query",
    ("contacts", "find"): "name",
    ("email_agent", "search"): "query",
}


def _args_for(skill: str, action: str, value: str) -> dict[str, Any]:
    """Put a routed value under the keyword the target actually accepts."""
    return {_ARG_NAMES.get((skill, action), "text"): value}


_CURLY = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}


def _normalize(text: str) -> str:
    """Punctuation-proof the utterance before matching.

    This router's input is mostly SPEECH. Whisper's punctuation is its own opinion: it writes
    "what's due" or "whats due" for identical audio, and prefers the curly apostrophe U+2019,
    which never matches a rule written with U+0027. Calvin asked "Whats due this week", the
    `what(?:'?s| is)? due` rule missed by one apostrophe, and the LLM fallback guessed
    find_events -- so he got a list of CTFs instead of his deadlines.

    Normalising here rather than in each rule means the next rule someone writes cannot
    reintroduce it.
    """
    out = (text or "").strip()
    for bad, good in _CURLY.items():
        out = out.replace(bad, good)
    return out


class IntentRouter:
    """Two-stage intent resolver. Inject an LLMClient for tests; defaults to the shared one."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    def route(self, text: str, *, use_llm: bool = True) -> Intent:
        """Resolve `text` to an Intent. Keyword rules first, LLM classify as fallback."""
        cleaned = _normalize(text)
        if not cleaned:
            return self._build("chit_chat", {}, confidence=0.0, via="keyword")

        for name, pattern, arg_key in _RULES:
            m = pattern.search(cleaned)
            if m:
                args: dict[str, Any] = {}
                if arg_key:
                    captured = _first_group(m)
                    if captured:
                        args[arg_key] = captured.strip()
                if name == "approve":
                    args = {"selection": _parse_selection(args.get("selection", cleaned))}
                log.debug("intent keyword-match: %s -> %s", cleaned, name)
                return self._build(name, args, confidence=0.9, via="keyword")

        if not use_llm:
            return self._build("chit_chat", {"text": cleaned}, confidence=0.2, via="keyword")

        # LLM fallback over the LIVE command catalogue, not a hardcoded label list.
        #
        # This is the "interpret and instruct" layer. The old fallback offered 13 fixed
        # labels, so 113 of the system's 151 skill commands were unreachable by talking to
        # it: `music.playlist` exists and works, but "create a playlist for coding" matched no
        # keyword rule, wasn't in the 13 labels, and fell through to chat -- which DESCRIBED
        # how to make a playlist instead of making one. The capability was never the problem;
        # nothing connected his words to it.
        #
        # Routing against what the system can actually do means a new skill becomes reachable
        # the moment it is registered, with no new regex and no edit here.
        catalogue = self._catalogue()
        if catalogue:
            picked = self._route_by_catalogue(cleaned, catalogue)
            if picked is not None:
                return picked

        label = self.llm.classify(
            cleaned,
            _LLM_LABELS,
            instruction="Pick the user's primary intent for a personal assistant.",
        )
        args = {"query": cleaned} if label == "research" else {"text": cleaned}
        log.debug("intent llm-match: %s -> %s", cleaned, label)
        return self._build(label, args, confidence=0.6, via="llm")

    def _catalogue(self) -> list[tuple[str, str, str]]:
        """(skill, action, one-line description) for everything currently registered."""
        try:
            from kernel.registry import get_registry

            registry = get_registry()
            out: list[tuple[str, str, str]] = []
            for name, skill in sorted(registry.skills.items()):
                for action, fn in skill.commands().items():
                    if action.startswith(("continue_", "_")) or action in _INTERNAL_ACTIONS:
                        continue        # continuations belong to a live session, not routing
                    doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
                    out.append((name, action, doc[0][:90] if doc else ""))
            return out
        except Exception:  # noqa: BLE001 - routing must survive a registry problem
            return []

    def _route_by_catalogue(self, text: str, catalogue: list[tuple[str, str, str]]) -> Intent | None:
        """Ask the model to pick a skill.action from what actually exists."""
        listing = "\n".join(f"{s}.{a}" + (f" — {d}" if d else "") for s, a, d in catalogue)
        try:
            data = self.llm.chat_json(
                "classify",
                [{"role": "system", "content":
                    "Route the user's message to ONE capability from the list, or to "
                    "chat.reply if none genuinely fits. Return the exact 'skill.action' "
                    "string. Prefer doing what was asked over talking about it: if the user "
                    "says 'create a playlist', route to the command that creates one. "
                    "`args` may carry a single obvious argument (a theme, a query, a name)."},
                 {"role": "user", "content":
                    f"Capabilities:\n{listing}\n\nMessage: {text}"}],
                schema_hint='{"target": "skill.action", "args": {"value": string}}',
                temperature=0.0, max_tokens=120)
        except LLMError:
            return None
        target = str((data or {}).get("target") or "")
        if "." not in target:
            return None
        skill, _, action = target.partition(".")
        if (skill, action) not in {(s, a) for s, a, _ in catalogue}:
            log.info("catalogue router picked an unknown target %r", target)
            return None
        value = str(((data or {}).get("args") or {}).get("value") or "").strip()
        args = _args_for(skill, action, value or text)
        log.info("intent catalogue-match: %s -> %s.%s", text, skill, action)
        return Intent(name=f"{skill}.{action}", skill=skill, action=action, args=args,
                      confidence=0.7, via="catalogue")

    @staticmethod
    def _build(name: str, args: dict[str, Any], *, confidence: float, via: str) -> Intent:
        skill, action = INTENTS.get(name, ("chat", "reply"))
        return Intent(name=name, skill=skill, action=action, args=args, confidence=confidence, via=via)


def _first_group(m: re.Match[str]) -> str | None:
    for g in m.groups():
        if g:
            return g
    return None


def _parse_selection(raw: str) -> list[int]:
    """Extract job/item numbers from phrasing like 'approve 1, 3 and 5'."""
    return [int(n) for n in re.findall(r"\d+", raw or "")]


_default_router: IntentRouter | None = None


def get_router() -> IntentRouter:
    global _default_router
    if _default_router is None:
        _default_router = IntentRouter()
    return _default_router
