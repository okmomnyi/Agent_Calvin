"""Desktop app control (Phase 23) — the SERVER half.

Calvin's laptop is the only machine that can open or close anything; this skill runs on the
droplet and cannot touch it. All it does is decide *which app key* an utterance refers to and
hand that key back to the voice client, which owns the allowlist and does the actual work
(`client/apps.py`).

Three things are structural, not policy:

1. **This skill emits an app KEY, never a command.** `{"op": "open", "app": "spotify"}` — never
   an executable, path, or argv. The droplet is internet-facing and its intent router is
   LLM-driven; if it could name a binary, a leaked AGENT_WS_TOKEN would be remote code
   execution on Calvin's laptop. The laptop maps keys to commands and refuses keys it doesn't
   know, so the worst a compromised server can do is ask for an app Calvin already approved.
2. **Closing is graceful.** The `close` op asks the app to exit the way clicking its X does, so
   an editor with unsaved work gets to prompt. There is no force-kill op — losing unsaved work
   is data loss, and §0 P4 does not carve out an exception for "it was only a text file".
3. **Actions are requests, not guarantees.** The laptop executes them if it's listening; the
   reply text must make sense either way (see `_confirmation`).

Config `desktop.apps` lists the keys the server considers plausible. It is a convenience for
phrasing ("I don't know that app") — NOT a security boundary. The boundary is the laptop's
allowlist, which re-checks every key it is handed.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.persona_store import get_engine
from core.skill import BaseSkill, CommandResult, SkillContract

log = get_logger("skills.desktop")

# Ops the laptop understands. No "kill"/"force" — see the module docstring.
OPS = ("open", "close", "focus")

# "open spotify" / "close vs code please" / "launch chrome"
_OPEN_RE = re.compile(r"\b(?:open|launch|start|fire up)\s+(?:the\s+|my\s+)?(.+)", re.I)
_CLOSE_RE = re.compile(r"\b(?:close|quit|exit|shut)\s+(?:down\s+)?(?:the\s+|my\s+)?(.+)", re.I)
_FOCUS_RE = re.compile(r"\b(?:focus|switch to|bring up|show me)\s+(?:the\s+|my\s+)?(.+)", re.I)

_TRAILING = re.compile(r"\b(please|now|for me|thanks|thank you)\b|[.!?,]", re.I)


def _norm(name: str) -> str:
    """'VS Code, please.' -> 'vs code' -> 'vs_code'. Keys are lowercase, underscore-joined."""
    cleaned = _TRAILING.sub("", name or "").strip()
    return re.sub(r"[\s\-]+", "_", cleaned.lower()).strip("_")


class DesktopSkill(BaseSkill):
    name = "desktop"

    def __init__(self, memory: Memory | None = None,
                 settings_fn: Callable[[], Any] | None = None) -> None:
        self._mem = memory
        self._settings = settings_fn or get_settings

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"open": self.open_app, "close": self.close_app,
                "focus": self.focus_app, "apps": self.apps}

    def contract(self) -> SkillContract:
        """Reads 'desktop' so rules like "never close my editor" have a reader.

        `never_force_kill` and `allowlisted_apps_only` are declared as hard invariants: no
        standing instruction, however phrased, can talk this skill into either.
        """
        return SkillContract(reads_categories=["desktop"],
                             hard_invariants=["allowlisted_apps_only", "never_force_kill"])

    # ------------------------------------------------------------- known keys
    def _known(self) -> list[str]:
        """App keys the server will name. NOT the security boundary — the laptop's is."""
        try:
            return [str(a) for a in (self._settings().get("desktop", "apps", default=[]) or [])]
        except Exception:  # noqa: BLE001
            return []

    def _resolve(self, spoken: str) -> str | None:
        """Map spoken text onto a configured key, or None if it isn't one we know.

        People say "vs code", not "code", so a key matches when its words are all present in
        what was said — `code` ⊆ {vs, code}. The containment runs that way round on purpose:
        matching by substring instead would make "code" resolve `vscode_insiders`, i.e. launch
        the wrong app. Ambiguity is refused rather than guessed, since a wrong guess here
        opens or closes something real.
        """
        key = _norm(spoken)
        if not key:
            return None
        known = self._known()
        if key in known:                      # exact key always wins
            return key
        said = set(key.split("_"))
        matches = [k for k in known if set(k.split("_")) <= said]
        if not matches:
            return None
        # Most specific wins: with keys `code` and `vs_code`, "vs code" means the latter.
        best = max(len(k.split("_")) for k in matches)
        finalists = [k for k in matches if len(k.split("_")) == best]
        return finalists[0] if len(finalists) == 1 else None      # tie -> ambiguous -> refuse

    # ------------------------------------------------------------- rules
    def _rules(self) -> list[str]:
        try:
            return get_engine().instructions_for_skill("desktop")
        except Exception:  # noqa: BLE001
            return []

    def _blocked_by_rule(self, op: str, app: str) -> str | None:
        """A standing rule may forbid closing a specific app. It can never unblock anything.

        Deliberately one-directional: rules here only ever *remove* capability. A rule that
        tried to grant force-killing would hit the `never_force_kill` invariant instead.
        """
        if op != "close":
            return None
        for rule in self._rules():
            r = rule.lower()
            if "close" not in r and "quit" not in r:
                continue
            if not any(neg in r for neg in ("never", "don't", "do not", "dont")):
                continue
            if app.replace("_", " ") in r.replace("_", " ") or app in r:
                return rule
        return None

    # ------------------------------------------------------------- actions
    @staticmethod
    def _action(op: str, app: str) -> dict[str, str]:
        assert op in OPS, f"unknown op {op!r}"          # never reachable from user input
        return {"op": op, "app": app}

    @staticmethod
    def _confirmation(op: str, app: str) -> str:
        pretty = app.replace("_", " ")
        return {"open": f"Opening {pretty}.", "close": f"Closing {pretty}.",
                "focus": f"Switching to {pretty}."}[op]

    def _dispatch(self, op: str, target: str) -> CommandResult:
        if not target:
            return CommandResult(text=f"Which app should I {op}?", ok=False)
        app = self._resolve(target)
        if app is None:
            known = ", ".join(k.replace("_", " ") for k in self._known()) or "nothing yet"
            return CommandResult(
                text=(f"I don't have \"{target.strip()}\" set up. I can reach: {known}. "
                      f"Add it to client/apps.yaml on the laptop."),
                data={"unknown_app": _norm(target)}, ok=False)

        blocked = self._blocked_by_rule(op, app)
        if blocked:
            return CommandResult(text=f"Not closing {app.replace('_', ' ')} — your standing "
                                      f"rule says: \"{blocked}\".",
                                 data={"blocked_by": blocked}, ok=False)

        return CommandResult(text=self._confirmation(op, app),
                             data={"client_actions": [self._action(op, app)], "app": app})

    def open_app(self, app: str = "", **_: Any) -> CommandResult:
        return self._dispatch("open", app)

    def close_app(self, app: str = "", **_: Any) -> CommandResult:
        return self._dispatch("close", app)

    def focus_app(self, app: str = "", **_: Any) -> CommandResult:
        return self._dispatch("focus", app)

    def apps(self, **_: Any) -> CommandResult:
        known = self._known()
        if not known:
            return CommandResult(text="No apps configured. Add them under `desktop.apps` in "
                                      "config.yaml and client/apps.yaml on the laptop.")
        listed = ", ".join(k.replace("_", " ") for k in known)
        return CommandResult(text=f"I can open, close or focus: {listed}.\n"
                                  f"(Your laptop has the final say — it re-checks every one.)",
                             data={"apps": known})


SKILL = DesktopSkill()
