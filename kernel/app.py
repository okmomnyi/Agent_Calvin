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
import inspect
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


def _enqueue_scheduled(job_id: str, skill: str, action: str):
    """Build the timer callback for a QUEUED job: enqueue it, don't run it here.

    The scheduler's job becomes "put work on the queue"; a worker does the work. That keeps
    scraping, transcription and batch LLM calls out of the API process, and gives them retries
    and visibility. Deduped on the job id, so a slow run still draining does not stack up
    another copy every time the timer fires.
    """
    def _fire() -> None:
        from core.queue import get_queue

        try:
            queued = get_queue().enqueue("skill.run", {"skill": skill, "action": action},
                                         dedupe_key=f"sched:{job_id}")
            log.info("scheduled '%s' -> %s", job_id,
                     f"queued #{queued}" if queued else "skipped (previous run still pending)")
        except Exception:  # noqa: BLE001 - a queue outage must not kill the scheduler
            log.exception("could not enqueue scheduled job '%s'", job_id)
    return _fire


def _register_scheduled_jobs() -> None:
    for job in registry.all_scheduled_jobs():
        try:
            func = job.func
            if getattr(job, "queued", False) and job.skill and job.action:
                func = _enqueue_scheduled(job.id, job.skill, job.action)
            scheduler.add_job(func, trigger=job.trigger, id=job.id, replace_existing=True, **job.kwargs)
            log.info("Registered scheduled job '%s' (%s)%s", job.id, job.trigger,
                     " [queued]" if getattr(job, "queued", False) else "")
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


def _handle_command(text: str, *, use_llm: bool = True, channel: str = "cli"):
    """Pass channel context when supported (test doubles from older phases need not accept it)."""
    handler = registry.handle_command
    if "channel" in inspect.signature(handler).parameters:
        return handler(text, use_llm=use_llm, channel=channel)
    return handler(text, use_llm=use_llm)


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
    intent, result = _handle_command(req.text, use_llm=req.use_llm, channel=req.channel)
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
    try:
        current = get_memory().current_plan(store.session_id)
        s["current_plan"] = ({"id": current["id"], "goal": current["goal"],
                              "status": current["status"]} if current else None)
    except Exception:  # noqa: BLE001 - session visibility must degrade, not fail
        s["current_plan"] = None
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


def _queue_stats() -> dict[str, int]:
    """Queue depth for /api/health. Never raises: health must work with the DB flaky."""
    try:
        from core.queue import get_queue

        return get_queue().stats()
    except Exception:  # noqa: BLE001
        return {}


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
async def api_voice(
    authorization: str | None = Header(default=None),
    x_agent_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the active pre-built voice + rate for the laptop client to synthesize with.

    Token-authed like every other /api/* route. It was public, which nothing needed: the
    client receives voice_id and rate on the /ws/voice reply itself and never calls this.
    """
    _authorize_remote_command(authorization, x_agent_token)
    return _current_voice()


@app.get("/api/health")
async def api_health(
    authorization: str | None = Header(default=None),
    x_agent_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Kernel health. Liveness is public; the detail requires the agent token.

    Split deliberately. The container HEALTHCHECK and any uptime monitor only need a 200 and
    ok/degraded, and requiring a token there would mean shipping the token to every probe.
    Everything else is a description of the deployment -- which capabilities exist, which
    credentials are configured, how deep the work queue is, what timezone the owner lives in
    -- and that is reconnaissance, not health. It was all served to anyone who asked, on the
    same box whose weekly recon scan exists to catch exactly this (§0 P12).
    """
    db_ok = True
    try:
        get_memory().conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.warning("Health: DB check failed: %s", exc)

    public = {
        "status": "ok" if (db_ok and scheduler.running) else "degraded",
        "scheduler_running": scheduler.running,
        "db_ok": db_ok,
    }

    try:
        _authorize_remote_command(authorization, x_agent_token)
    except HTTPException:
        return public          # unauthenticated probes get liveness, and nothing else

    settings = get_settings()
    try:
        from core.gmail_client import GmailClient

        gmail_token = GmailClient.token_status()
    except Exception as exc:  # noqa: BLE001
        gmail_token = {"present": None, "error": str(exc)}

    return {
        **public,
        # Phase 26: a backlog or a
        # pile of failures should be visible here, not discovered in a log.
        "queue": _queue_stats(),
        "scheduled_jobs": len(scheduler.get_jobs()) if scheduler.running else 0,
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

            intent, result = _handle_command(text, channel="voice")
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
