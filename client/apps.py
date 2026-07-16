"""Desktop app control (Phase 23) — the LAPTOP half, and the security boundary.

The droplet can only ever say `{"op": "open", "app": "spotify"}`. This module decides what
"spotify" means on THIS machine, and refuses anything that isn't in `apps.yaml`. That
direction matters: the server is internet-facing and LLM-driven, the laptop is not. If the
server could hand over a command string, a leaked AGENT_WS_TOKEN would be remote code
execution here. It can't — so the worst it can do is ask for an app Calvin already approved.

Consequently:

* **argv lists only, never a shell.** Every command comes from apps.yaml as a list and is run
  with `shell=False`. Nothing from the server is ever interpolated into a command — it is only
  ever used as a dict key. A key that isn't in the map is refused before anything runs.
* **`close` is graceful, and there is no force op.** Windows gets `taskkill /IM` *without* /F
  (which posts WM_CLOSE, so an editor can still prompt about unsaved work); macOS gets an
  AppleScript `quit`; Linux gets SIGTERM. Force-killing loses unsaved work, and §0 P4 doesn't
  make an exception for "it was only a text file".
* **Failures are reported, never raised into the voice loop.** A missing app shouldn't take the
  client down mid-sentence.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

# Ops we accept from the server. Anything else is refused — this is a whitelist, not a filter.
OPS = ("open", "close", "focus")

APPS_FILE = Path(__file__).with_name("apps.yaml")


def current_os() -> str:
    """'windows' | 'darwin' | 'linux' — the key apps.yaml is written against."""
    return {"Windows": "windows", "Darwin": "darwin"}.get(platform.system(), "linux")


@dataclass
class Outcome:
    ok: bool
    detail: str

    def __str__(self) -> str:  # what the voice client prints
        return ("" if self.ok else "! ") + self.detail


def load_allowlist(path: Path | None = None) -> dict[str, dict]:
    """Parse apps.yaml. A malformed or missing file means NOTHING is allowed, not everything."""
    p = path or APPS_FILE
    if not p.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        apps = data.get("apps") or {}
        return {str(k): v for k, v in apps.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 — fail closed
        return {}


class AppController:
    """Executes server-requested app ops against the local allowlist."""

    def __init__(self, allowlist: dict[str, dict] | None = None, os_name: str | None = None,
                 runner: Callable[[list[str]], int] | None = None,
                 spawner: Callable[[list[str]], None] | None = None) -> None:
        self._apps = load_allowlist() if allowlist is None else allowlist
        self._os = os_name or current_os()
        self._run = runner or _run
        self._spawn = spawner or _spawn

    # ------------------------------------------------------------- lookup
    def known(self) -> list[str]:
        return sorted(self._apps)

    def _entry(self, app: str) -> dict | None:
        return self._apps.get(app)

    def _argv(self, app: str, field: str) -> list[str] | None:
        """Pull a per-OS argv list out of the allowlist, or None if unsupported here."""
        entry = self._entry(app) or {}
        value = (entry.get(field) or {}).get(self._os)
        if isinstance(value, str):          # tolerate a bare string in yaml
            value = [value]
        if not value or not all(isinstance(x, str) for x in value):
            return None
        return list(value)

    # ------------------------------------------------------------- ops
    def execute(self, action: dict[str, Any]) -> Outcome:
        """Run one `{"op": ..., "app": ...}` from the server. Never raises."""
        op, app = str(action.get("op", "")), str(action.get("app", ""))
        if op not in OPS:
            return Outcome(False, f"refused: unknown op {op!r}")
        if app not in self._apps:
            # The refusal that makes the whole design safe.
            return Outcome(False, f"refused: {app!r} is not in apps.yaml")
        try:
            return getattr(self, f"_{op}")(app)
        except Exception as exc:  # noqa: BLE001 — a bad app must not kill the voice loop
            return Outcome(False, f"{op} {app} failed: {exc}")

    def execute_all(self, actions: Iterable[dict[str, Any]]) -> list[Outcome]:
        return [self.execute(a) for a in (actions or [])]

    def _open(self, app: str) -> Outcome:
        argv = self._argv(app, "launch")
        if not argv:
            return Outcome(False, f"{app} has no launch command for {self._os}")
        self._spawn(argv)                    # detached: don't block the voice loop on a GUI
        return Outcome(True, f"opened {app}")

    def _close(self, app: str) -> Outcome:
        """Graceful only. See the module docstring — there is deliberately no force path."""
        argv = self._argv(app, "close")
        if not argv:
            proc = (self._entry(app) or {}).get("process", {}).get(self._os)
            if not proc:
                return Outcome(False, f"{app} has no close command for {self._os}")
            argv = _default_close(self._os, proc)
        code = self._run(argv)
        return (Outcome(True, f"closed {app}") if code == 0
                else Outcome(False, f"{app} didn't close (exit {code}) — it may be prompting "
                                    f"about unsaved work"))

    def _focus(self, app: str) -> Outcome:
        argv = self._argv(app, "focus")
        if argv:
            self._spawn(argv)
            return Outcome(True, f"focused {app}")
        # No focus command? Re-launching almost always raises an existing window, and is
        # harmless for single-instance apps — better than pretending we can't.
        argv = self._argv(app, "launch")
        if not argv:
            return Outcome(False, f"{app} has no focus command for {self._os}")
        self._spawn(argv)
        return Outcome(True, f"focused {app}")


def _default_close(os_name: str, process: str) -> list[str]:
    """The graceful close for each platform. NOTE: no /F, no -9, no SIGKILL. Deliberate."""
    if os_name == "windows":
        # Without /F this posts WM_CLOSE — the app can still save/prompt.
        return ["taskkill", "/IM", process]
    if os_name == "darwin":
        return ["osascript", "-e", f'quit app "{process}"']
    return ["pkill", "-TERM", "-x", process]     # SIGTERM, not SIGKILL


def _run(argv: list[str]) -> int:
    """Run and wait. shell=False is the point — argv never goes through a shell."""
    if not shutil.which(argv[0]):
        raise FileNotFoundError(f"{argv[0]} not found on PATH")
    return subprocess.run(argv, shell=False, capture_output=True, timeout=20).returncode


def _spawn(argv: list[str]) -> None:
    """Launch detached: a GUI app must not hold the voice loop open."""
    subprocess.Popen(argv, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
