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

from core.llm import LLMClient, get_client
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
    "summarize_inbox": ("email_agent", "digest"),
    "trash_email":     ("email_agent", "trash"),
    "restore_email":   ("email_agent", "restore"),
    "summarize":       ("router", "summarize"),
    "find_jobs":       ("job_hunter", "hunt"),
    "find_events":     ("event_scout", "find"),
    "event_interested": ("event_scout", "interested"),
    "event_skip":       ("event_scout", "skip"),
    "job_status":      ("job_hunter", "status"),
    "approve":         ("approvals", "approve"),
    "review_code":     ("code_review", "review"),
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
    ("tutor", re.compile(r"\btutor(?: mode)?\b[:,]?\s*(?P<t>.+)", re.I), "topic"),
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
    ("trash_email", re.compile(
        r"\b(?:delete|trash|remove|clear|clean\s+(?:up|out)|get\s+rid\s+of)\b"
        r"(?P<t>.*\bemails?\b.*)", re.I), "query"),
    ("trash_email", re.compile(
        r"\b(?:delete|trash|remove|clear)\b(?P<t>.*)", re.I), "query"),
    ("check_email", re.compile(r"\bcheck (?:my )?(?:email|inbox|mail)\b|\bany (?:new )?email\b", re.I), None),
    # Desktop app control (Phase 23) — laptop-side, voice only. Deliberately late in the list
    # and anchored to the end of the utterance: "start"/"open" are common verbs elsewhere
    # ("start a mock interview"), so these must never win ahead of a real skill. An app we
    # don't know still lands here and gets an honest "I don't have that set up".
    ("list_apps", re.compile(r"\b(?:what|which) apps\b|\blist apps\b", re.I), None),
    ("open_app", re.compile(
        r"\b(?:open|launch|fire up)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    ("close_app", re.compile(
        r"\b(?:close|quit|shut down)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    ("focus_app", re.compile(
        r"\b(?:focus|switch to|bring up)\s+(?:the\s+|my\s+)?(?P<a>[\w .+-]{1,30})$", re.I), "app"),
    ("summarize", re.compile(r"\bsummari[sz]e\b\s*(?P<t>.+)", re.I), "target"),
    ("research", re.compile(r"\b(?:search|research|look up|find out)(?: for| about)?\s+(?P<t>.+)", re.I), "query"),
]

# Labels offered to the LLM fallback classifier (only when keywords miss).
_LLM_LABELS = [
    "research", "check_email", "summarize_inbox", "find_jobs", "find_events",
    "job_status", "review_code", "answer_form", "tailor_cv", "remember",
    "ask_notes", "quiz", "chit_chat",
]


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

        # LLM fallback — strict single label
        label = self.llm.classify(
            cleaned,
            _LLM_LABELS,
            instruction="Pick the user's primary intent for a personal assistant.",
        )
        args = {"query": cleaned} if label == "research" else {"text": cleaned}
        log.debug("intent llm-match: %s -> %s", cleaned, label)
        return self._build(label, args, confidence=0.6, via="llm")

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
