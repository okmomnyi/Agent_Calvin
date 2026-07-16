"""FastAPI kernel for AgentOS.

Exposes /ws/voice (token-authenticated WebSocket for the laptop voice client),
/api/command (token-authenticated REST dispatch for the phone shortcut), and /api/health.
On startup it discovers skills, registers their scheduled jobs with APScheduler
(Africa/Nairobi tz), and starts the scheduler. The API process is designed to run
independently — a skill raising never brings the kernel down.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import get_memory
from kernel.registry import SkillRegistry

log = get_logger("kernel.app")

registry = SkillRegistry()
scheduler = AsyncIOScheduler(timezone=get_settings().tz)


def _register_scheduled_jobs() -> None:
    for job in registry.all_scheduled_jobs():
        try:
            scheduler.add_job(job.func, trigger=job.trigger, id=job.id, replace_existing=True, **job.kwargs)
            log.info("Registered scheduled job '%s' (%s)", job.id, job.trigger)
        except Exception:  # noqa: BLE001
            log.exception("Could not register scheduled job '%s'", job.id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AgentOS kernel starting up…")
    registry.discover()
    get_memory()  # ensure schema exists
    _register_scheduled_jobs()
    if not scheduler.running:
        scheduler.start()
    log.info("AgentOS kernel ready.")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    log.info("AgentOS kernel shut down.")


app = FastAPI(title="AgentOS", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ voice helpers
_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"\*([^*]*)\*"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
]


def to_spoken(text: str, max_chars: int = 700) -> str:
    """Strip markdown and shorten a reply so it's pleasant to hear (kernel voice-friendly)."""
    out = text
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    out = re.sub(r"\n{2,}", ". ", out).replace("\n", " ")
    out = re.sub(r"\s{2,}", " ", out).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


# ------------------------------------------------------------------ REST
class CommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    spoken: bool = False
    use_llm: bool = True
    channel: str = "cli"          # telegram | voice | dashboard | cli (Phase 19 continuity)


class CommandResponse(BaseModel):
    ok: bool
    text: str
    intent: str
    skill: str
    via: str
    data: dict[str, Any] = Field(default_factory=dict)


def _authorize_remote_command(authorization: str | None, x_agent_token: str | None) -> None:
    """Require the shared agent token for the remotely exposed command endpoint."""
    expected = get_settings().ws_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Remote command authentication is not configured.",
        )

    supplied = x_agent_token or ""
    if authorization:
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            supplied = credential.strip()

    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/api/command", response_model=CommandResponse)
async def api_command(
    req: CommandRequest,
    authorization: str | None = Header(default=None),
    x_agent_token: str | None = Header(default=None),
) -> CommandResponse:
    """Authenticate and route a text command through the intent router and target skill."""
    _authorize_remote_command(authorization, x_agent_token)
    intent, result = registry.handle_command(req.text, use_llm=req.use_llm)
    text = to_spoken(result.text) if req.spoken else result.text
    _record_turn(req.text, text, req.channel, intent.skill)
    return CommandResponse(
        ok=result.ok, text=text, intent=intent.name, skill=intent.skill, via=intent.via, data=result.data
    )


def _record_turn(text: str, reply: str, channel: str, skill: str) -> None:
    """Append the exchange to the ONE server-side session, whatever channel it came from."""
    try:
        from core.session import SessionStore

        SessionStore().record_turn(text, reply, channel, skill)
    except Exception:  # noqa: BLE001 - continuity must never break a reply
        log.debug("could not record session turn", exc_info=True)


@app.get("/api/session")
async def api_session(
    authorization: str | None = Header(default=None),
    x_agent_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """The shared session: live skill, last channel, recent turns, pending approvals."""
    _authorize_remote_command(authorization, x_agent_token)
    from core.session import SessionStore

    store = SessionStore()
    s = store.get()
    s["live_skill_session"] = store.live_skill_session()
    return s


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """The 4th channel: a browser UI on the same kernel API — no client install anywhere.

    The page is static; every action it takes is token-authed against /api/* like any client.
    """
    return (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")


def _current_voice() -> dict[str, Any]:
    """Look up the active pre-built voice/rate from the voice skill (safe default if absent)."""
    skill = registry.get("voice")
    if skill is not None and hasattr(skill, "current"):
        try:
            return skill.current()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    return {"voice": "guy", "voice_id": "en-US-GuyNeural", "rate": "+0%", "rate_percent": 0}


def _client_actions(result: Any) -> list[dict[str, str]]:
    """App ops a skill wants the laptop to run (Phase 23).

    Re-validated here rather than trusted: a skill returns plain dicts, and only `op`/`app`
    ever reach the wire — so a stray key (a path, an argv) can't be smuggled through to the
    laptop even by a buggy skill. The laptop checks the app against its own allowlist again
    regardless; this is just the narrow waist.
    """
    from skills.desktop import OPS

    out: list[dict[str, str]] = []
    for action in (getattr(result, "data", {}) or {}).get("client_actions") or []:
        if not isinstance(action, dict):
            continue
        op, app = str(action.get("op", "")), str(action.get("app", ""))
        if op in OPS and app:
            out.append({"op": op, "app": app})
    return out


@app.get("/api/voice")
async def api_voice() -> dict[str, Any]:
    """Return the active pre-built voice + rate for the laptop client to synthesize with."""
    return _current_voice()


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Report kernel subsystem health: scheduler, DB, NIM key, skills discovered."""
    settings = get_settings()
    db_ok = True
    try:
        get_memory().conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.warning("Health: DB check failed: %s", exc)

    try:
        from core.gmail_client import GmailClient

        gmail_token = GmailClient.token_status()
    except Exception as exc:  # noqa: BLE001
        gmail_token = {"present": None, "error": str(exc)}

    return {
        "status": "ok" if (db_ok and scheduler.running) else "degraded",
        "scheduler_running": scheduler.running,
        "scheduled_jobs": len(scheduler.get_jobs()) if scheduler.running else 0,
        "db_ok": db_ok,
        "nim_key_present": bool(settings.nvidia_api_key),
        "gmail_token": gmail_token,
        "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "skills": sorted(registry.skills.keys()),
        "timezone": settings.tz,
    }


# ------------------------------------------------------------------ WebSocket (voice)
@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    """Token-authed voice channel. Client sends {token, text}; server replies spoken text.

    Auth: token may be given as ?token= query param or in the first JSON message.
    """
    await websocket.accept()
    settings = get_settings()
    token = websocket.query_params.get("token", "")

    try:
        while True:
            msg = await websocket.receive_json()
            token = msg.get("token") or token
            if (not settings.ws_token or not token
                    or not secrets.compare_digest(token, settings.ws_token)):
                await websocket.send_json({"ok": False, "text": "Unauthorized."})
                await websocket.close(code=4401)
                return

            text = (msg.get("text") or "").strip()
            if not text:
                await websocket.send_json({"ok": False, "text": "I didn't catch that."})
                continue

            intent, result = registry.handle_command(text)
            _record_turn(text, result.text, "voice", intent.skill)
            voice = _current_voice()
            await websocket.send_json(
                {
                    "ok": result.ok,
                    "text": to_spoken(result.text),
                    "intent": intent.name,
                    "skill": intent.skill,
                    # client speaks with this pre-built voice/rate (updates instantly on "change voice")
                    "voice_id": voice["voice_id"],
                    "rate": voice["rate"],
                    # Phase 23: app ops for the LAPTOP to run — {"op": ..., "app": ...} keys
                    # only, never commands. The laptop re-checks each against its own allowlist
                    # and refuses anything it doesn't know; see client/apps.py. Only /ws/voice
                    # carries these — the phone and dashboard can't reach the laptop.
                    "actions": _client_actions(result),
                }
            )
    except WebSocketDisconnect:
        log.debug("Voice websocket disconnected.")
