"""FastAPI kernel for AgentOS.

Exposes /ws/voice (ticket-authenticated WebSocket for the browser dashboard and the laptop
voice client), /api/command (session-authenticated REST dispatch), /api/auth/* (real login —
break-glass password, WebAuthn passkeys, recovery codes), and /api/health. On startup it
discovers skills, registers their scheduled jobs with APScheduler (Africa/Nairobi tz), and
starts the scheduler. The API process is designed to run independently — a skill raising
never brings the kernel down.
"""

from __future__ import annotations

import re
import inspect
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import (Depends, FastAPI, HTTPException, Request, Response, WebSocket,
                     WebSocketDisconnect)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import get_memory
from kernel.registry import SkillRegistry

log = get_logger("kernel.app")

registry = SkillRegistry()
scheduler = AsyncIOScheduler(timezone=get_settings().tz)

# Phase 36 (rebuilt on React/Vite): one source tree (frontend/) renders both the web shell
# (served here) and the desktop shell (pywebview loads the same built index.html off disk).
# `frontend/` is now Vite SOURCE (TypeScript, unbuilt) -- what gets served is the `vite
# build` output, `frontend/dist/`, whose every asset path is relative (vite.config.ts's
# `base: './'`) so it works identically served from here or loaded off local disk.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


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
    from core.config import seed_data_warnings

    try:
        for warning in seed_data_warnings(get_settings()):
            log.warning("unfilled config: %s", warning)
    except Exception:  # noqa: BLE001 - a startup check must never block the kernel coming up
        log.debug("could not check for seed/placeholder config", exc_info=True)
    if get_settings().get("auth", "dev_insecure", default=False):
        log.warning("auth.dev_insecure is ON — session cookies are being issued WITHOUT the "
                    "Secure flag. This must never be true in production; it exists only so a "
                    "local Vite dev server on plain HTTP can carry the session cookie.")
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


# ------------------------------------------------------------------ auth (Slice 0a-0e)
# Replaces AGENT_WS_TOKEN everywhere. The browser dashboard uses the session cookie + WS
# tickets (0a/0b); the laptop voice client uses a long-lived device credential + the same
# WS ticket flow (0e) — no code path reads AGENT_WS_TOKEN anymore.
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _cookie_secure() -> bool:
    """False only when auth.dev_insecure is explicitly on (local Vite dev over plain HTTP,
    which cannot carry a Secure cookie at all). Loud by construction: lifespan() logs a
    warning on every startup while this is set, so it can't linger unnoticed into a real
    deploy. Fails toward Secure (never toward insecure) if Settings can't even answer the
    question — some tests use a minimal fake Settings with no `.get()` at all, and the safe
    default here is the opposite of seed_data_warnings' own try/except elsewhere in this file.
    """
    try:
        return not get_settings().get("auth", "dev_insecure", default=False)
    except AttributeError:
        return True


def require_session(request: Request) -> dict[str, Any]:
    """FastAPI dependency guarding every /api/* route except /api/auth/* and /api/health.

    Reads the httpOnly session cookie, validates it against auth_sessions, and returns the
    live session row. Deliberately no WWW-Authenticate header on failure — this is a cookie
    session, not HTTP Basic/Bearer, and offering that scheme back would be misleading.
    """
    from core.auth import SESSION_COOKIE_NAME, get_store

    raw = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_store().validate_session(raw or "")
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return session


def _check_lockout(kind: str, request: Request) -> None:
    """Raises 429 (with Retry-After) if this (kind, ip) is in backoff or lockout (Slice
    0d). A passive read — never itself recorded as an attempt, so polling while locked out
    cannot extend the lockout."""
    from core.auth import get_store

    locked, retry_after = get_store().lockout_status(kind, _client_ip(request))
    if locked:
        raise HTTPException(
            status_code=429, detail="Too many attempts — try again shortly.",
            headers={"Retry-After": str(max(1, int(retry_after) + 1))})


class PasswordLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=500)


@app.post("/api/auth/password")
async def auth_password_login(
    req: PasswordLoginRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Break-glass login (Slice 0a). Passkey login (the primary factor) arrives in 0c."""
    from core.auth import SESSION_COOKIE_NAME, SESSION_MAX_LIFETIME, get_store

    _check_lockout("password", request)
    store = get_store()
    ip = _client_ip(request)
    ok = store.verify_password(req.password)
    store.record_attempt("password", ok, ip=ip)
    if not ok:
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token = store.create_session(user_agent=request.headers.get("user-agent", ""), ip=ip)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, secure=_cookie_secure(), samesite="strict",
        max_age=SESSION_MAX_LIFETIME, path="/",
    )
    return {"ok": True}


class RecoveryLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


@app.post("/api/auth/recovery")
async def auth_recovery_login(
    req: RecoveryLoginRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Single-use recovery-code login (Slice 0d) — the break-glass password's own
    break-glass, for "I lost my passkey device AND forgot the password" days."""
    from core.auth import SESSION_COOKIE_NAME, SESSION_MAX_LIFETIME, get_store

    _check_lockout("recovery", request)
    store = get_store()
    ip = _client_ip(request)
    ok = store.verify_recovery_code(req.code)
    store.record_attempt("recovery", ok, ip=ip)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid or already-used recovery code.")
    token = store.create_session(user_agent=request.headers.get("user-agent", ""), ip=ip)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, secure=_cookie_secure(), samesite="strict",
        max_age=SESSION_MAX_LIFETIME, path="/",
    )
    return {
        "ok": True,
        "notice": "Logged in with a recovery code — register a fresh passkey soon.",
        "remaining_recovery_codes": store.unused_recovery_code_count(),
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    from core.auth import SESSION_COOKIE_NAME, get_store

    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw:
        get_store().revoke_session_by_token(raw)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


# ------------------------------------------------------------------ WebAuthn (Slice 0c)
def _is_localhost(request: Request) -> bool:
    return _client_ip(request) in ("127.0.0.1", "::1", "localhost")


def _webauthn_rp_id() -> str:
    """The registrable domain — also Caddy's {$AGENTOS_DOMAIN}. "localhost" is the one
    value browsers treat as a secure context without real TLS, which is what makes local
    dev/testing of this endpoint possible before a real subdomain exists."""
    return get_settings().agentos_domain or "localhost"


def _webauthn_origin(request: Request) -> str:
    """Trust the browser's own Origin header over reconstructing one from settings — a
    mismatch there is exactly what verify_registration/authentication_response checks for,
    so guessing wrong here would just turn a real security check into a false negative."""
    origin = request.headers.get("origin")
    if origin:
        return origin
    domain = get_settings().agentos_domain
    if domain:
        return f"https://{domain}"
    return f"http://{_client_ip(request) or 'localhost'}:{request.url.port or 80}"


def _require_first_run_or_session(request: Request) -> None:
    """The bootstrap guard (item 1 of the spec): registering the very FIRST credential
    when nothing else is configured yet may only happen from localhost / the droplet
    console — never the public origin, so an attacker can't register their own passkey
    before Calvin does. Once a password or a passkey already exists, adding another
    device is an ordinary logged-in action instead."""
    from core.auth import get_store

    store = get_store()
    if not store.has_credential() and not store.has_password():
        if not _is_localhost(request):
            raise HTTPException(
                status_code=403,
                detail="First-time setup must be completed from the droplet console "
                       "or localhost, not the public origin.")
        return
    require_session(request)


class RegisterPasskeyOptionsRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)


@app.post("/api/auth/webauthn/register/options")
async def webauthn_register_options(
    req: RegisterPasskeyOptionsRequest, request: Request,
) -> dict[str, Any]:
    from webauthn.helpers import options_to_json_dict

    from core.auth import WebAuthnError, get_store

    _require_first_run_or_session(request)
    try:
        options = get_store().start_registration(
            rp_id=_webauthn_rp_id(), user_name=get_settings().my_name)
    except WebAuthnError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return options_to_json_dict(options)


class RegisterPasskeyVerifyRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    credential: dict[str, Any]


@app.post("/api/auth/webauthn/register/verify")
async def webauthn_register_verify(
    req: RegisterPasskeyVerifyRequest, request: Request, response: Response,
) -> dict[str, Any]:
    from core.auth import SESSION_COOKIE_NAME, SESSION_MAX_LIFETIME, WebAuthnError, get_store

    _require_first_run_or_session(request)
    store = get_store()
    transports = (req.credential.get("response") or {}).get("transports") or []
    try:
        store.verify_registration(
            req.credential, expected_rp_id=_webauthn_rp_id(),
            expected_origin=_webauthn_origin(request), label=req.label, transports=transports)
    except WebAuthnError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Registering the FIRST-EVER passkey also logs you in — otherwise the bootstrap flow
    # would register a credential and then immediately demand a login with it anyway.
    if not request.cookies.get(SESSION_COOKIE_NAME):
        token = store.create_session(user_agent=request.headers.get("user-agent", ""),
                                     ip=_client_ip(request))
        response.set_cookie(
            SESSION_COOKIE_NAME, token, httponly=True, secure=_cookie_secure(), samesite="strict",
            max_age=SESSION_MAX_LIFETIME, path="/")
    return {"ok": True}


@app.post("/api/auth/webauthn/login/options")
async def webauthn_login_options() -> dict[str, Any]:
    from webauthn.helpers import options_to_json_dict

    from core.auth import WebAuthnError, get_store

    try:
        options = get_store().start_login(rp_id=_webauthn_rp_id())
    except WebAuthnError as exc:
        # "No passkey registered" is not a server error — the caller falls back to the
        # break-glass password, so this is an ordinary, expected 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return options_to_json_dict(options)


class LoginPasskeyVerifyRequest(BaseModel):
    credential: dict[str, Any]


@app.post("/api/auth/webauthn/login/verify")
async def webauthn_login_verify(
    req: LoginPasskeyVerifyRequest, request: Request, response: Response,
) -> dict[str, Any]:
    from webauthn import base64url_to_bytes

    from core.auth import SESSION_COOKIE_NAME, SESSION_MAX_LIFETIME, WebAuthnError, get_store

    _check_lockout("passkey", request)
    store = get_store()
    ip = _client_ip(request)
    raw_id = req.credential.get("rawId") or req.credential.get("id") or ""
    try:
        credential_id = base64url_to_bytes(raw_id)
        token = store.verify_login(
            req.credential, credential_id, expected_rp_id=_webauthn_rp_id(),
            expected_origin=_webauthn_origin(request),
            user_agent=request.headers.get("user-agent", ""), ip=ip)
    except WebAuthnError as exc:
        store.record_attempt("passkey", False, ip=ip)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    store.record_attempt("passkey", True, ip=ip)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, secure=_cookie_secure(), samesite="strict",
        max_age=SESSION_MAX_LIFETIME, path="/")
    return {"ok": True}


@app.post("/api/auth/ws-ticket")
async def auth_ws_ticket(
    auth_session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """Mint a single-use, ~15s WS ticket (Slice 0b) — requires a valid session cookie.

    This is the REST half of the fix: auth happens here, over a normal request that CAN
    carry the cookie; the socket then opens with the ticket instead of anything reusable.
    """
    from core.auth import get_store

    ticket = get_store().mint_ws_ticket(auth_session["id"])
    return {"ticket": ticket}


# ------------------------------------------------------------------ approvals + step-up (0d)
class ApprovalResolveRequest(BaseModel):
    approve: bool
    always: bool = False
    # A fresh WebAuthn assertion (from /api/auth/webauthn/login/options + a browser
    # navigator.credentials.get(), NOT re-sent from the original login) — required for a
    # high-tier action when step_up_on_high is on and a passkey is registered.
    step_up_credential: dict[str, Any] | None = None


@app.post("/api/approvals/{action_id}/resolve")
async def approvals_resolve(
    action_id: int, req: ApprovalResolveRequest, request: Request,
    auth_session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """Dashboard path to ApprovalStore.resolve() — today only Telegram's text-reply parser
    (`skills/telegram_bot.py`'s `_try_approval_reply`) can resolve a pending_actions row;
    this is the same call, reached over REST, so the browser has a route to reach it too.

    This is NOT a second approval mechanism (§0 P3's own requirement) — both paths end at
    the identical `ApprovalStore.resolve()`. For a `high`-tier action, with step_up_on_high
    on and a passkey registered, this additionally demands a FRESH assertion in the same
    request before it will call resolve() at all; deny is exempt (§0 P3 already lets a
    denial through unlearned — refusing something never needs proof of presence). With no
    passkey registered, step-up is skipped entirely and the ordinary approval goes through,
    so Calvin can never be locked out of his own gate.
    """
    from core.approvals import TIER_HIGH, get_store as get_approval_store
    from core.auth import WebAuthnError, get_store as get_auth_store

    approvals = get_approval_store()
    action = approvals.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Unknown action id.")

    if (action.tier == TIER_HIGH and req.approve
            and get_settings().get("auth", "step_up_on_high", default=True)):
        auth_store = get_auth_store()
        if auth_store.has_credential():
            if not req.step_up_credential:
                raise HTTPException(status_code=401, detail="step_up_required")
            from webauthn import base64url_to_bytes

            raw_id = (req.step_up_credential.get("rawId")
                     or req.step_up_credential.get("id") or "")
            try:
                credential_id = base64url_to_bytes(raw_id)
                auth_store.verify_step_up(
                    req.step_up_credential, credential_id, expected_rp_id=_webauthn_rp_id(),
                    expected_origin=_webauthn_origin(request))
            except WebAuthnError as exc:
                raise HTTPException(
                    status_code=401, detail=f"step-up failed: {exc}") from exc

    resolved = approvals.resolve(action_id, req.approve, always=req.always)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown action id.")
    return {"ok": True, "status": resolved.status}


@app.post("/api/command", response_model=CommandResponse)
async def api_command(
    req: CommandRequest,
    session: dict[str, Any] = Depends(require_session),
) -> CommandResponse:
    """Authenticate and route a text command through the intent router and target skill."""
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
    auth_session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """The shared session: live skill, last channel, recent turns, pending approvals."""
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


@app.get("/api/jobs")
async def api_jobs(
    auth_session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """Job listings for the dashboard's browse panel -- the same drafted/notified set
    skills/telegram_bot.py's jobs_payload() shows, just with room for more than 10 rows
    since this is a dedicated scrollable card rather than a chat message."""
    mem = get_memory()
    rows = mem.jobs_by_status("notified", limit=40) + mem.jobs_by_status("drafted", limit=40)
    rows = sorted(rows, key=lambda r: r["score"] or 0, reverse=True)[:40]
    jobs = [
        {"id": r["id"], "title": r["title"], "company": r["company"], "score": r["score"],
         "category": r["category"], "status": r["status"], "apply_kind": r["apply_kind"],
         "apply_target": r["apply_target"]}
        for r in rows
    ]
    try:
        total = sum(mem.count_jobs_by_status(s) for s in ("notified", "drafted"))
    except Exception:  # noqa: BLE001 - a count must never break the listing
        total = len(jobs)
    return {"jobs": jobs, "total": total}


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


# Format-only sanity check for the narrow waist below — deliberately NOT skills/contacts.py's
# full phonenumbers-backed `normalize_phone` (which needs a region and does a heavier check).
# This mirrors client/adb_bridge.py's own independent copy of the same pattern: each side of
# the trust boundary re-validates on its own terms rather than sharing one implementation
# across server and laptop, which stay independently deployable on purpose (Phase 26).
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _client_actions(result: Any) -> list[dict[str, str]]:
    """Client-bound ops a skill wants the laptop to run (Phase 23, extended Phase 36).

    Re-validated here rather than trusted: a skill returns plain dicts, and only the fields
    each recognized op actually needs ever reach the wire — so a stray key (a path, an argv)
    can't be smuggled through to the laptop even by a buggy skill. Every op is ALSO
    re-checked against its own allowlist/validation on the laptop side regardless; this is
    just the narrow waist. Unrecognized ops are dropped silently rather than passed through,
    so a new op only ever reaches the wire once it has a case here.
    """
    from skills.desktop import OPS as APP_OPS
    from skills.web_open import validate_url

    out: list[dict[str, str]] = []
    for action in (getattr(result, "data", {}) or {}).get("client_actions") or []:
        if not isinstance(action, dict):
            continue
        op = str(action.get("op", ""))
        if op in APP_OPS:
            app = str(action.get("app", ""))
            if app:
                out.append({"op": op, "app": app})
        elif op == "open_url":
            url = str(action.get("url", ""))
            if url and validate_url(url) is None:
                out.append({"op": op, "url": url})
        elif op == "call":
            number = str(action.get("number", ""))
            if number and _E164_RE.match(number):
                out.append({"op": op, "number": number})
        elif op in ("answer", "hangup"):
            out.append({"op": op})
    return out


@app.get("/api/voice")
async def api_voice(
    auth_session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    """Return the active pre-built voice + rate for the laptop client to synthesize with.

    Session-authed like every other /api/* route. It was public, which nothing needed: the
    client receives voice_id and rate on the /ws/voice reply itself and never calls this.
    """
    return _current_voice()


@app.get("/api/health")
async def api_health(request: Request) -> dict[str, Any]:
    """Kernel health. Liveness is public; the detail requires a logged-in session.

    Split deliberately. The container HEALTHCHECK and any uptime monitor only need a 200 and
    ok/degraded, and requiring a session there would mean the probe would have to log in.
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
        require_session(request)
    except HTTPException:
        return public          # unauthenticated probes get liveness, and nothing else

    settings = get_settings()
    try:
        from core.gmail_client import GmailClient

        gmail_token = GmailClient.token_status()
    except Exception as exc:  # noqa: BLE001
        gmail_token = {"present": None, "error": str(exc)}

    from core.config import seed_data_warnings

    try:
        seed_warnings = seed_data_warnings(settings)
    except Exception:  # noqa: BLE001 - a sub-check must never take down /api/health itself
        seed_warnings = []

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
        # Config that still matches the shipped example text -- never actually filled in, and
        # otherwise silently reaches the daily briefing forever (§0: never fabricate).
        "seed_data_warnings": seed_warnings,
    }


# ------------------------------------------------------------------ WebSocket (voice)
@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    """Ticket-authed voice channel (Slice 0b, laptop migrated in 0e).

    `?ticket=<raw>` — a single-use, ~15s credential minted over REST after a real login
    (POST /api/auth/ws-ticket), which requires a valid session (a browser's cookie, or the
    laptop voice client's long-lived device credential riding as a Cookie header — see
    core/auth.py's issue_device_credential). This is the actual fix for "the WS doesn't
    connect correctly": a browser cannot set an Authorization header on `new WebSocket()`,
    so any REST bearer scheme is unusable here, and the old approach put a long-lived,
    reusable AGENT_WS_TOKEN in the URL instead — no code path reads that anymore. The ticket
    is consumed atomically at handshake time; a second connection attempt with the same raw
    value fails, and the underlying session is re-checked on every message so a revoked
    session (browser or device) drops the socket immediately rather than at the next
    reconnect.
    """
    from core.auth import get_store

    await websocket.accept()

    ticket = websocket.query_params.get("ticket", "")
    auth_session_id = get_store().consume_ws_ticket(ticket) if ticket else None
    if auth_session_id is None:
        await websocket.send_json({"ok": False, "text": "Unauthorized."})
        await websocket.close(code=4401)
        return

    try:
        while True:
            msg = await websocket.receive_json()

            # Already proved identity at handshake via the ticket; only re-check the
            # session is still live, so a revoke elsewhere ends this connection too.
            if get_store().session_by_id(auth_session_id) is None:
                await websocket.send_json({"ok": False, "text": "Session expired — log in again."})
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


# ------------------------------------------------------------------ dashboard (Phase 36)
# The 4th channel: a browser UI on the same kernel API — no client install anywhere.
# `html=True` serves frontend/dist/index.html at /dashboard/ and every built asset under it
# at its own relative path. The page itself is static; every action it takes is
# session-authed against /api/* exactly like any other client. Mounted last so it never
# shadows an /api/* or /ws/voice route defined above.
#
# StaticFiles raises at construction if FRONTEND_DIR is missing -- which would otherwise
# crash the WHOLE kernel (every skill, /api/health, everything) just because someone forgot
# `npm run build` in frontend/. The API staying up without a dashboard is a far smaller
# problem than the API not staying up at all, so this degrades instead of dying.
if FRONTEND_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
else:
    log.warning(
        "frontend/dist not found — /dashboard will 404 until `npm run build` runs in "
        "frontend/. Every other route (skills, /api/*, /ws/voice) is unaffected.")
