"""Real authentication for AgentOS (replaces the static AGENT_WS_TOKEN).

Single user (Calvin), so there is no `users` table -- a passkey, the break-glass password,
and a session simply belong to the one account. Built in slices: 0a (break-glass password +
session cookies), 0b (WS ticket flow), 0c (WebAuthn passkeys, this module's newest section).
Rate-limit enforcement (0d) still just logs via `record_attempt`.

Every secret here is stored HASHED. The raw value exists exactly once: in the response that
issues it (a Set-Cookie header, a printed recovery code), never again in the database. §0 P4
("never delete") extends to auth data in spirit -- sessions are revoked (`revoked_at`), the
password keeps its prior rows rather than overwriting them, a passkey is revoked not deleted,
so a compromised credential's history stays inspectable even after it stops working.

WebAuthn stores ONLY a public key and an opaque credential id (§0: no biometric data ever
touches this server -- Face ID / Windows Hello unlock happens on-device, and the platform
authenticator never sends the template anywhere). The registration/login ceremony functions
take the verify/generate calls as injectable parameters (defaulting to the real `webauthn`
library) so tests exercise this module's own logic -- credential storage, challenge
single-use, sign_count regression -- against a fake, the same way an LLM or HTTP fetcher is
faked elsewhere in this codebase, rather than needing real authenticator hardware or
recorded attestation bytes to run the test suite.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.auth")

SESSION_COOKIE_NAME = "agentos_session"
SESSION_IDLE_TIMEOUT = 7 * 24 * 3600     # 7 days of inactivity ends a session early
SESSION_MAX_LIFETIME = 30 * 24 * 3600    # 30 days absolute, however active
DEVICE_MAX_LIFETIME = 365 * 24 * 3600    # a headless device credential (Slice 0e): 1 year,
                                          # no idle timeout, explicitly issued and revocable
WS_TICKET_TTL = 15                       # seconds: minted, handed to the socket, burned
WEBAUTHN_CHALLENGE_TTL = 300             # 5 minutes to complete a passkey ceremony
WEBAUTHN_RP_NAME = "AgentOS"
PASSWORD_RESET_OTP_TTL = 600             # 10 minutes: minted, emailed, must be used by then

_hasher = PasswordHasher()


class WebAuthnError(ValueError):
    """A passkey ceremony failed — bad/expired challenge, verification failure, or a
    sign_count regression. Callers turn this into an HTTP 400/401, never a 500; a failed
    ceremony is an expected outcome, not a server bug."""


def _hash_token(raw: str) -> str:
    """Session tokens are high-entropy random values, not user-chosen secrets -- a fast
    hash is the right tool here. Argon2's deliberate slowness is for the password and
    recovery codes, where an attacker might brute-force a low-entropy guess; there is
    nothing to brute-force about a 32-byte `secrets.token_urlsafe` value."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_ip(ip: str) -> str:
    """IPs are never stored raw (privacy). Deterministic (not per-run-salted) on purpose --
    rate-limiting needs repeat attempts from the same source to hash the same way."""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32] if ip else ""


class AuthStore:
    def __init__(self, memory: Memory | None = None, clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    # ------------------------------------------------------------- break-glass password
    def set_password(self, raw_password: str) -> None:
        if not raw_password or len(raw_password) < 12:
            raise ValueError("password must be at least 12 characters")
        hashed = _hasher.hash(raw_password)
        with self.mem.tx() as conn:
            # INSERT, never UPDATE/DELETE the prior row -- §0 P4 in spirit. verify_password()
            # only ever reads the newest row, so this is invisible in normal use.
            conn.execute(
                "INSERT INTO credential_password(argon2_hash, updated_at) VALUES(%s,%s)",
                (hashed, self._now()))

    def _current_password_hash(self) -> str | None:
        row = self.mem.execute(
            "SELECT argon2_hash FROM credential_password ORDER BY id DESC LIMIT 1").fetchone()
        return row["argon2_hash"] if row else None

    def has_password(self) -> bool:
        return self._current_password_hash() is not None

    def verify_password(self, raw_password: str) -> bool:
        current = self._current_password_hash()
        if not current:
            return False
        try:
            _hasher.verify(current, raw_password)
            return True
        except VerifyMismatchError:
            return False
        except Exception:  # noqa: BLE001 - a malformed stored hash must not crash login
            log.exception("password verification failed unexpectedly")
            return False

    # ------------------------------------------------------------- sessions
    def create_session(self, *, user_agent: str = "", ip: str = "", device: bool = False) -> str:
        """Mint a new session. Returns the RAW token — the caller sets it as the cookie
        value (or, for a device credential, pastes it into the laptop's .env) and must not
        persist it anywhere else; only its hash is ever stored.

        `device=True` mints a long-lived (1 year), idle-timeout-exempt credential for a
        headless client instead of a browser session — see issue_device_credential().
        """
        raw = secrets.token_urlsafe(32)
        now = self._now()
        lifetime = DEVICE_MAX_LIFETIME if device else SESSION_MAX_LIFETIME
        with self.mem.tx() as conn:
            conn.execute(
                "INSERT INTO auth_sessions(session_token_hash, created_at, last_seen_at, "
                "expires_at, user_agent, ip_hash, is_device) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (_hash_token(raw), now, now, now + lifetime,
                 (user_agent or "")[:300], hash_ip(ip), int(device)))
        return raw

    def issue_device_credential(self, label: str) -> str:
        """A named, revocable, long-lived credential for a headless client (the laptop
        voice client). Returns the RAW token — shown exactly once, same contract as a
        recovery code; only its hash is ever stored."""
        return self.create_session(user_agent=f"device:{label}"[:300], device=True)

    def validate_session(self, raw_token: str) -> dict[str, Any] | None:
        """The live session for this cookie/device-credential value, or None if
        missing/revoked/expired.

        Slides `last_seen_at` forward on every valid use. A browser session additionally
        enforces the idle timeout, so a stolen-but-unused cookie doesn't stay valid for the
        full 30 days just because it hasn't hit its outer expiry yet — a device credential
        is exempt from that (an always-on background client legitimately goes quiet for
        days without that meaning anything), and relies on its own year-long absolute
        lifetime plus being explicitly revocable instead.
        """
        if not raw_token:
            return None
        row = self.mem.execute(
            "SELECT * FROM auth_sessions WHERE session_token_hash=%s",
            (_hash_token(raw_token),)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        now = self._now()
        idle_timeout_hit = (not row["is_device"]
                           and now - row["last_seen_at"] > SESSION_IDLE_TIMEOUT)
        if now > row["expires_at"] or idle_timeout_hit:
            return None
        with self.mem.tx() as conn:
            conn.execute("UPDATE auth_sessions SET last_seen_at=%s WHERE id=%s", (now, row["id"]))
        return dict(row)

    def revoke_session(self, session_id: int) -> None:
        with self.mem.tx() as conn:
            conn.execute("UPDATE auth_sessions SET revoked_at=%s WHERE id=%s",
                         (self._now(), session_id))

    def revoke_session_by_token(self, raw_token: str) -> bool:
        row = self.mem.execute(
            "SELECT id FROM auth_sessions WHERE session_token_hash=%s",
            (_hash_token(raw_token),)).fetchone()
        if row is None:
            return False
        self.revoke_session(row["id"])
        return True

    # ------------------------------------------------------------- WS tickets (Slice 0b)
    def mint_ws_ticket(self, session_id: int) -> str:
        """A single-use, ~15s-lived credential for the /ws/voice handshake.

        This is the actual fix for "the WS doesn't connect correctly": a browser cannot set
        an Authorization header on `new WebSocket()`, so any REST bearer scheme is unusable
        there, and the old static AGENT_WS_TOKEN filled that gap with a long-lived value
        riding in a URL — a browser-history-and-proxy-log liability. A ticket is worthless
        the instant it's used or 15 seconds pass, so replaying the URL buys nothing.
        """
        raw = secrets.token_urlsafe(24)
        now = self._now()
        with self.mem.tx() as conn:
            conn.execute(
                "INSERT INTO ws_tickets(ticket_hash, session_id, created_at, expires_at) "
                "VALUES(%s,%s,%s,%s)",
                (_hash_token(raw), session_id, now, now + WS_TICKET_TTL))
        return raw

    def consume_ws_ticket(self, raw_ticket: str) -> int | None:
        """Redeem a ticket for the auth_session id it was minted for, or None.

        One atomic UPDATE ... WHERE used_at IS NULL AND expires_at > now RETURNING — not a
        read-then-write — so two simultaneous handshakes racing the same ticket can never
        both succeed; Postgres's row lock during the UPDATE serializes them, and the loser
        sees zero rows affected.
        """
        if not raw_ticket:
            return None
        now = self._now()
        with self.mem.tx() as conn:
            row = conn.execute(
                "UPDATE ws_tickets SET used_at=%s "
                "WHERE ticket_hash=%s AND used_at IS NULL AND expires_at > %s "
                "RETURNING session_id",
                (now, _hash_token(raw_ticket), now)).fetchone()
        return int(row["session_id"]) if row else None

    def session_by_id(self, session_id: int) -> dict[str, Any] | None:
        """Live-session check by id — used to drop a ticket-authenticated WS mid-connection
        if the underlying session gets revoked, without needing the raw cookie again."""
        row = self.mem.execute(
            "SELECT * FROM auth_sessions WHERE id=%s", (session_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        now = self._now()
        if now > row["expires_at"] or now - row["last_seen_at"] > SESSION_IDLE_TIMEOUT:
            return None
        return dict(row)

    # ------------------------------------------------------------- passkeys (Slice 0c)
    def add_credential(self, credential_id: bytes, public_key: bytes, sign_count: int, *,
                       transports: str = "", label: str) -> int:
        with self.mem.tx() as conn:
            row = conn.execute(
                "INSERT INTO credentials(credential_id, public_key, sign_count, transports, "
                "label, created_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (credential_id, public_key, sign_count, transports, label,
                 self._now())).fetchone()
        return int(row["id"])

    def get_credential(self, credential_id: bytes) -> dict[str, Any] | None:
        return self.mem.execute(
            "SELECT * FROM credentials WHERE credential_id=%s AND revoked_at IS NULL",
            (credential_id,)).fetchone()

    def active_credentials(self) -> list[dict[str, Any]]:
        return self.mem.execute(
            "SELECT * FROM credentials WHERE revoked_at IS NULL ORDER BY id").fetchall()

    def has_credential(self) -> bool:
        return bool(self.active_credentials())

    def update_sign_count(self, credential_id: bytes, new_count: int) -> None:
        with self.mem.tx() as conn:
            conn.execute(
                "UPDATE credentials SET sign_count=%s, last_used_at=%s WHERE credential_id=%s",
                (new_count, self._now(), credential_id))

    def revoke_credential(self, credential_id: bytes) -> None:
        with self.mem.tx() as conn:
            conn.execute("UPDATE credentials SET revoked_at=%s WHERE credential_id=%s",
                         (self._now(), credential_id))

    # ---- ceremony challenges ----
    # Stored in `kv`, not a dedicated table: a WebAuthn challenge is a single ephemeral
    # value that exists only between the options call and the verify call of ONE ceremony.
    # `kind` ('register' | 'login') keeps the two ceremonies from being able to satisfy each
    # other's challenge. Single-user, single-tab-at-a-time in practice, so the very small
    # race of two concurrent ceremonies overwriting each other's challenge is accepted
    # rather than engineered around — the loser's verify simply fails and they retry.
    def _store_challenge(self, kind: str, challenge: bytes) -> None:
        payload = json.dumps({"challenge": base64.b64encode(challenge).decode("ascii"),
                              "expires_at": self._now() + WEBAUTHN_CHALLENGE_TTL})
        self.mem.kv_set(f"webauthn.challenge.{kind}", payload)

    def _consume_challenge(self, kind: str) -> bytes | None:
        key = f"webauthn.challenge.{kind}"
        raw = self.mem.kv_get(key)
        if not raw:
            return None
        self.mem.kv_set(key, "")   # single-use: clear immediately, win or lose
        try:
            data = json.loads(raw)
            if self._now() > data["expires_at"]:
                return None
            return base64.b64decode(data["challenge"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def start_registration(self, *, rp_id: str, user_name: str,
                           generate_fn: Callable[..., Any] | None = None) -> Any:
        """Step 1 of registering a new device passkey. Returns the options object the
        endpoint serializes to JSON for the browser's `navigator.credentials.create()`."""
        from webauthn import generate_registration_options
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                               PublicKeyCredentialDescriptor,
                                               UserVerificationRequirement)

        generate = generate_fn or generate_registration_options
        # user_verification=REQUIRED is the "gated by the device's own biometric/PIN" half
        # of the goal -- a bare security-key tap with no PIN/biometric would not satisfy this.
        options = generate(
            rp_id=rp_id, rp_name=WEBAUTHN_RP_NAME, user_name=user_name,
            user_display_name=user_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=c["credential_id"])
                for c in self.active_credentials()],
        )
        self._store_challenge("register", options.challenge)
        return options

    def verify_registration(self, credential: Any, *, expected_rp_id: str,
                            expected_origin: str, label: str, transports: list[str] | None = None,
                            verify_fn: Callable[..., Any] | None = None) -> int:
        """Step 2: verify the browser's response and store the new credential.

        `credential` is whatever `navigator.credentials.create()` returned, JSON-decoded —
        the `webauthn` library parses it internally and doesn't hand back the structured
        form, so `transports` (purely informational) is passed separately; the endpoint
        reads it straight off the same raw dict at `body["response"]["transports"]`.

        Raises WebAuthnError on any failure. Returns the new credential's row id.
        """
        from webauthn import verify_registration_response

        challenge = self._consume_challenge("register")
        if challenge is None:
            raise WebAuthnError("no pending registration ceremony (expired or never started)")
        verify = verify_fn or verify_registration_response
        try:
            result = verify(credential=credential, expected_challenge=challenge,
                            expected_rp_id=expected_rp_id, expected_origin=expected_origin,
                            require_user_verification=True)
        except Exception as exc:  # noqa: BLE001 - the library raises its own exception types
            raise WebAuthnError(f"registration verification failed: {exc}") from exc
        return self.add_credential(
            result.credential_id, result.credential_public_key, result.sign_count,
            transports=",".join(transports or []), label=label)

    def start_login(self, *, rp_id: str,
                    generate_fn: Callable[..., Any] | None = None) -> Any:
        """Step 1 of a passkey login. Returns the options object for
        `navigator.credentials.get()`. Refuses if no passkey is registered — the caller
        falls back to the break-glass password, never a dead end."""
        from webauthn import generate_authentication_options
        from webauthn.helpers.structs import (PublicKeyCredentialDescriptor,
                                               UserVerificationRequirement)

        creds = self.active_credentials()
        if not creds:
            raise WebAuthnError("no passkey registered")
        generate = generate_fn or generate_authentication_options
        options = generate(
            rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=c["credential_id"])
                              for c in creds])
        self._store_challenge("login", options.challenge)
        return options

    def _verify_assertion(self, credential: Any, credential_id: bytes, *, expected_rp_id: str,
                          expected_origin: str,
                          verify_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Shared by verify_login() and step-up (0d): consume the login challenge, verify
        the assertion, and enforce the sign_count regression check. Returns the credential
        row (with its sign_count now updated) on success; raises WebAuthnError otherwise.

        A synced passkey (iCloud Keychain, Google Password Manager) legitimately reports
        sign_count=0 forever, since a counter can't be kept consistent across synced copies
        of the same credential — per the WebAuthn spec's own guidance, a regression is only
        flagged once the count has been seen to be meaningful (nonzero), never for a
        credential stuck at 0.
        """
        from webauthn import verify_authentication_response

        challenge = self._consume_challenge("login")
        if challenge is None:
            raise WebAuthnError("no pending login ceremony (expired or never started)")
        stored = self.get_credential(credential_id)
        if stored is None:
            raise WebAuthnError("unknown or revoked credential")
        verify = verify_fn or verify_authentication_response
        try:
            result = verify(credential=credential, expected_challenge=challenge,
                            expected_rp_id=expected_rp_id, expected_origin=expected_origin,
                            credential_public_key=stored["public_key"],
                            credential_current_sign_count=stored["sign_count"],
                            require_user_verification=True)
        except Exception as exc:  # noqa: BLE001
            raise WebAuthnError(f"assertion verification failed: {exc}") from exc

        if stored["sign_count"] != 0 and result.new_sign_count < stored["sign_count"]:
            log.warning("sign_count regression on credential %s — possible clone",
                       stored["id"])
            raise WebAuthnError("sign count went backwards — possible cloned authenticator")

        self.update_sign_count(credential_id, result.new_sign_count)
        stored["sign_count"] = result.new_sign_count
        return stored

    def verify_login(self, credential: Any, credential_id: bytes, *, expected_rp_id: str,
                     expected_origin: str, user_agent: str = "", ip: str = "",
                     verify_fn: Callable[..., Any] | None = None) -> str:
        """Step 2 of a passkey login: verify the assertion and mint a session. Raises
        WebAuthnError on any failure, including a sign_count regression (clone detection —
        the one check this slice exists to add)."""
        self._verify_assertion(credential, credential_id, expected_rp_id=expected_rp_id,
                               expected_origin=expected_origin, verify_fn=verify_fn)
        return self.create_session(user_agent=user_agent, ip=ip)

    def verify_step_up(self, credential: Any, credential_id: bytes, *, expected_rp_id: str,
                       expected_origin: str,
                       verify_fn: Callable[..., Any] | None = None) -> bool:
        """A FRESH assertion required in front of a high-tier approval (0d) — proves "a
        touch/glance happened just now" without minting a new session, since the caller is
        already logged in. Same verification (and the same clone check) as a full login;
        the only difference is what happens on success."""
        self._verify_assertion(credential, credential_id, expected_rp_id=expected_rp_id,
                               expected_origin=expected_origin, verify_fn=verify_fn)
        return True

    # ------------------------------------------------------------- recovery codes (0d)
    # No 0/O/1/l/i: characters that are easy to mis-type or mis-read off a printed slip.
    _RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

    def generate_recovery_codes(self, n: int = 10) -> list[str]:
        """Generate N single-use break-glass codes. Returns the RAW codes — the ONLY time
        they are ever visible; only argon2 hashes are stored. Does not touch any
        still-unused codes from a previous call (§0 P4 in spirit): call this again and you
        simply have two valid sets until the old ones are used or you revoke them."""
        codes = []
        with self.mem.tx() as conn:
            for _ in range(n):
                code = "-".join(
                    "".join(secrets.choice(self._RECOVERY_ALPHABET) for _ in range(5))
                    for _ in range(3))
                codes.append(code)
                conn.execute(
                    "INSERT INTO recovery_codes(code_hash, created_at) VALUES(%s,%s)",
                    (_hasher.hash(code), self._now()))
        return codes

    def verify_recovery_code(self, raw_code: str) -> bool:
        """Single-use: the first still-unused code whose hash matches is marked used.

        Argon2 hashes are salted per-value, so there is no equality lookup — this checks
        against each unused code in turn, which is fine at the scale of a handful of codes.
        The UPDATE ... WHERE used_at IS NULL RETURNING guards the race where two requests
        try to redeem the same code at once: only one can win.
        """
        raw_code = (raw_code or "").strip()
        if not raw_code:
            return False
        for row in self.mem.execute(
                "SELECT id, code_hash FROM recovery_codes WHERE used_at IS NULL").fetchall():
            try:
                _hasher.verify(row["code_hash"], raw_code)
            except VerifyMismatchError:
                continue
            except Exception:  # noqa: BLE001 - a malformed stored hash must not crash login
                continue
            with self.mem.tx() as conn:
                marked = conn.execute(
                    "UPDATE recovery_codes SET used_at=%s WHERE id=%s AND used_at IS NULL "
                    "RETURNING id", (self._now(), row["id"])).fetchone()
            return marked is not None
        return False

    def unused_recovery_code_count(self) -> int:
        row = self.mem.execute(
            "SELECT COUNT(*) c FROM recovery_codes WHERE used_at IS NULL").fetchone()
        return row["c"]

    # ------------------------------------------------------------- password reset OTP (Phase 36)
    # Telegram is already a single-authorized-chat channel, but that alone is not enough proof
    # to hand out a fresh account password over it -- this is the second factor: a short-lived
    # code that only reaches an inbox Telegram itself has no access to.
    def request_password_reset_otp(self) -> str:
        """Mint a 6-digit OTP. Returns the RAW code -- the caller emails it and never stores
        or echoes it anywhere else; only its argon2 hash is kept. Any still-unused code from
        an earlier request is superseded (marked used) so an old email can never be replayed
        once a newer one has been sent."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = self._now()
        with self.mem.tx() as conn:
            conn.execute(
                "UPDATE password_reset_otp SET used_at=%s WHERE used_at IS NULL", (now,))
            conn.execute(
                "INSERT INTO password_reset_otp(code_hash, created_at, expires_at) "
                "VALUES(%s,%s,%s)",
                (_hasher.hash(code), now, now + PASSWORD_RESET_OTP_TTL))
        return code

    def verify_password_reset_otp(self, raw_code: str) -> bool:
        """Single-use and time-limited, same shape as verify_recovery_code(). Brute-force
        protection is the caller's job (record_attempt/lockout_status with kind
        'password_reset_otp'), same as every other human-typed secret here."""
        raw_code = (raw_code or "").strip()
        if not raw_code:
            return False
        now = self._now()
        rows = self.mem.execute(
            "SELECT id, code_hash FROM password_reset_otp "
            "WHERE used_at IS NULL AND expires_at > %s", (now,)).fetchall()
        for row in rows:
            try:
                _hasher.verify(row["code_hash"], raw_code)
            except VerifyMismatchError:
                continue
            except Exception:  # noqa: BLE001 - a malformed stored hash must not crash this
                continue
            with self.mem.tx() as conn:
                marked = conn.execute(
                    "UPDATE password_reset_otp SET used_at=%s WHERE id=%s AND used_at IS NULL "
                    "RETURNING id", (now, row["id"])).fetchone()
            return marked is not None
        return False

    def revoke_all_sessions(self, *, keep_devices: bool = True) -> int:
        """Sign out every active session -- used right after a password reset, since resetting
        the master credential should end every other logged-in place, not just rotate the
        password quietly. Device credentials (the laptop/voice client) were issued separately
        and explicitly, so they survive by default; pass keep_devices=False to drop those too.
        Returns how many sessions were revoked."""
        now = self._now()
        with self.mem.tx() as conn:
            if keep_devices:
                rows = conn.execute(
                    "UPDATE auth_sessions SET revoked_at=%s "
                    "WHERE revoked_at IS NULL AND is_device=0 RETURNING id", (now,)).fetchall()
            else:
                rows = conn.execute(
                    "UPDATE auth_sessions SET revoked_at=%s "
                    "WHERE revoked_at IS NULL RETURNING id", (now,)).fetchall()
        return len(rows)

    # ------------------------------------------------------------- attempts + lockout (0d)
    # Password/recovery are the weaker factors (a guessable secret) so they lock out harder
    # and faster than passkey (already gated by physical possession + device biometric).
    _LOCKOUT_THRESHOLDS = {
        "password": (5, 900),             # 5 failures -> 15 min lockout
        "recovery": (3, 1800),            # 3 failures -> 30 min lockout
        "passkey": (10, 300),             # 10 failures -> 5 min lockout
        "password_reset_otp": (5, 900),   # 5 failures -> 15 min lockout, same weight as password
    }
    _BACKOFF_CAP = 60.0  # seconds

    def record_attempt(self, kind: str, ok: bool, *, ip: str = "") -> None:
        with self.mem.tx() as conn:
            conn.execute(
                "INSERT INTO auth_attempts(kind, ok, ip_hash, at) VALUES(%s,%s,%s,%s)",
                (kind, ok, hash_ip(ip), self._now()))

    def lockout_status(self, kind: str, ip: str) -> tuple[bool, float]:
        """(locked_out, retry_after_seconds). Two stages, both time-based and BOTH clear on
        their own — never a permanent hard-fail:

        * below the threshold: a growing minimum gap between attempts (2^failures seconds,
          capped) — the "exponential backoff" the spec asks for;
        * at/past the threshold: a hard lockout for the rest of the window.

        Checking this never itself counts as an attempt (record_attempt is a separate,
        explicit call) — so polling while locked out cannot extend the lockout, which is
        what keeps this from ever becoming "permanent" under sustained hammering.
        """
        threshold, window = self._LOCKOUT_THRESHOLDS.get(kind, (5, 900))
        since = self._now() - window
        rows = self.mem.execute(
            "SELECT ok, at FROM auth_attempts WHERE kind=%s AND ip_hash=%s AND at >= %s "
            "ORDER BY at DESC", (kind, hash_ip(ip), since)).fetchall()
        consecutive = 0
        last_failure_at = None
        for row in rows:
            if row["ok"]:
                break
            consecutive += 1
            if last_failure_at is None:
                last_failure_at = row["at"]
        if consecutive == 0:
            return False, 0.0
        if consecutive >= threshold:
            return True, window
        backoff = min(2 ** consecutive, self._BACKOFF_CAP)
        elapsed = self._now() - last_failure_at
        if elapsed < backoff:
            return True, backoff - elapsed
        return False, 0.0


def get_store(memory: Memory | None = None) -> AuthStore:
    return AuthStore(memory=memory)
