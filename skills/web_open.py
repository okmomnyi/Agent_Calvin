"""Open URLs (Phase 36 Slice 3) — named favourites (server-side) opened laptop-side.

Same shape as Phase 23's desktop-app-control design: this skill only ever hands the laptop
a URL, never executes anything itself, and every URL is re-validated against the scheme
allowlist a second time server-side (kernel/app.py's `_client_actions`) before it reaches
the wire — belt and braces, the same "narrow waist" pattern `skills/desktop.py` uses for
app keys.

Execution itself (Slice 2's bridge, via Python's `webbrowser` module — cross-platform,
never `os.system('start ...')`) lives in client/hud_window.py; this module only decides
*which* URL and *whether it's allowed at all*.
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlparse

from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, SkillContract

log = get_logger("skills.web_open")

# http/https only (build-prompt constraint). `file:`, `javascript:`, `data:` and anything
# else are refused outright — no exceptions, no per-instruction override.
ALLOWED_SCHEMES = {"http", "https"}


def validate_url(url: str) -> str | None:
    """Return an error string if `url` must be refused, else None."""
    url = (url or "").strip()
    if not url:
        return "No URL given."
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"\"{url}\" doesn't parse as a URL."
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return f"Refusing to open a \"{parsed.scheme or '?'}:\" URL — only http/https are allowed."
    if not parsed.netloc:
        return f"\"{url}\" has no host."
    return None


class WebOpenSkill(BaseSkill):
    name = "web_open"

    def __init__(self, memory: Memory | None = None) -> None:
        self._mem = memory

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"add": self.add, "list": self.list_favourites, "open": self.open,
                "retire": self.retire}

    def contract(self) -> SkillContract:
        return SkillContract(reads_categories=[])

    # ------------------------------------------------------------- favourites
    def add(self, name: str = "", url: str = "", **_: Any) -> CommandResult:
        name = (name or "").strip()
        if not name:
            return CommandResult(text="A favourite needs a name.", ok=False)
        error = validate_url(url)
        if error:
            return CommandResult(text=error, ok=False)
        url = url.strip()
        now = time.time()
        with self.mem.tx() as conn:
            conn.execute(
                "INSERT INTO url_favourites(name, url, created_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url, retired_at=NULL",
                (name, url, now))
        return CommandResult(text=f"Saved \"{name}\".", data={"name": name, "url": url})

    def list_favourites(self, **_: Any) -> CommandResult:
        rows = [dict(r) for r in self.mem.execute(
            "SELECT name, url FROM url_favourites WHERE retired_at IS NULL "
            "ORDER BY name").fetchall()]
        if not rows:
            return CommandResult(text="No saved URLs yet.", data={"favourites": []})
        return CommandResult(text="\n".join(f"{r['name']}: {r['url']}" for r in rows),
                             data={"favourites": rows})

    def retire(self, name: str = "", **_: Any) -> CommandResult:
        name = (name or "").strip()
        with self.mem.tx() as conn:
            cur = conn.execute(
                "UPDATE url_favourites SET retired_at=%s WHERE name=%s AND retired_at IS NULL",
                (time.time(), name))
        if cur.rowcount == 0:
            return CommandResult(text=f"No active favourite named \"{name}\".", ok=False)
        return CommandResult(text=f"Retired \"{name}\".")

    # ------------------------------------------------------------- open
    def open(self, name: str = "", url: str = "", **_: Any) -> CommandResult:
        """Resolve to a URL (a saved favourite by name, or a raw URL) and hand it to the
        laptop as a client action. `open_url` — never a command, never a path — is the same
        narrow-waist shape Phase 23 established for app control."""
        target = url.strip() if url else ""
        label = (name or "").strip()
        if not target and label:
            row = self.mem.execute(
                "SELECT url FROM url_favourites WHERE name=%s AND retired_at IS NULL",
                (label,)).fetchone()
            if not row:
                known = ", ".join(r["name"] for r in self.mem.execute(
                    "SELECT name FROM url_favourites WHERE retired_at IS NULL "
                    "ORDER BY name").fetchall())
                return CommandResult(
                    text=(f"I don't have \"{label}\" saved."
                          + (f" I have: {known}." if known else "")),
                    ok=False)
            target = row["url"]
        error = validate_url(target)
        if error:
            return CommandResult(text=error, ok=False)
        return CommandResult(text=f"Opening {label or target}.",
                             data={"client_actions": [{"op": "open_url", "url": target}]})


SKILL = WebOpenSkill()
