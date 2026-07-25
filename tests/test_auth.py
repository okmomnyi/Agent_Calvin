"""Real login: break-glass password + session cookies (0a), WS tickets are covered in
tests/test_kernel.py (0b), WebAuthn passkeys (0c) below.

The guardrails that matter here: secrets are stored HASHED, never raw; a session enforces
BOTH an absolute lifetime and an idle timeout; a revoked session stops working immediately
even if its raw token is replayed; nothing is ever deleted -- only revoked/superseded.

The WebAuthn tests inject fake generate/verify functions rather than exercising the real
`webauthn` library's cryptography — that library has its own test suite, and a real ceremony
needs an actual authenticator + browser + secure context anyway (none of which pytest has).
What's under test here is AgentOS's OWN logic: challenge single-use, credential storage, and
specifically the sign_count regression check, which is the one guardrail this slice exists
to add ("a decreasing counter means a cloned authenticator — reject").
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from core.auth import SESSION_IDLE_TIMEOUT, SESSION_MAX_LIFETIME, AuthStore, WebAuthnError


@pytest.fixture
def store(mem):
    return AuthStore(memory=mem, clock=lambda: 1_000_000.0)


# ================================================================= break-glass password
def test_password_is_stored_argon2_hashed_never_raw(store, mem):
    store.set_password("correct horse battery staple")
    row = mem.execute("SELECT argon2_hash FROM credential_password ORDER BY id DESC LIMIT 1").fetchone()
    assert row["argon2_hash"] != "correct horse battery staple"
    assert row["argon2_hash"].startswith("$argon2")


def test_verify_password_accepts_the_right_one_and_rejects_others(store):
    store.set_password("correct horse battery staple")
    assert store.verify_password("correct horse battery staple") is True
    assert store.verify_password("wrong password entirely") is False


def test_verify_password_with_none_set_is_false_not_a_crash(store):
    assert store.has_password() is False
    assert store.verify_password("anything") is False


def test_set_password_rejects_short_passwords(store):
    with pytest.raises(ValueError):
        store.set_password("short")


def test_setting_a_new_password_never_deletes_the_old_row(store, mem):
    """§0 P4 in spirit: a compromised password's history stays inspectable."""
    store.set_password("first password here please")
    store.set_password("second password here please")
    rows = mem.execute("SELECT COUNT(*) c FROM credential_password").fetchone()
    assert rows["c"] == 2
    assert store.verify_password("second password here please") is True
    assert store.verify_password("first password here please") is False


# ================================================================= sessions
def test_session_token_is_stored_hashed_never_raw(store, mem):
    raw = store.create_session(user_agent="pytest", ip="203.0.113.5")
    row = mem.execute("SELECT session_token_hash FROM auth_sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["session_token_hash"] != raw
    assert len(row["session_token_hash"]) == 64   # sha256 hex digest


def test_ip_is_stored_hashed_never_raw(store, mem):
    store.create_session(ip="203.0.113.5")
    row = mem.execute("SELECT ip_hash FROM auth_sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ip_hash"] != "203.0.113.5"
    assert "203.0.113.5" not in (row["ip_hash"] or "")


def test_a_fresh_session_validates(store):
    raw = store.create_session()
    session = store.validate_session(raw)
    assert session is not None


def test_missing_or_garbage_token_does_not_validate(store):
    store.create_session()
    assert store.validate_session("") is None
    assert store.validate_session("not-a-real-token") is None


def test_revoked_session_stops_validating_even_with_the_right_raw_token(store):
    raw = store.create_session()
    assert store.validate_session(raw) is not None
    session = store.validate_session(raw)
    store.revoke_session(session["id"])
    assert store.validate_session(raw) is None


def test_revoke_by_token_finds_and_revokes_the_right_session(store):
    raw = store.create_session()
    assert store.revoke_session_by_token(raw) is True
    assert store.validate_session(raw) is None


def test_revoke_by_token_is_a_safe_no_op_for_an_unknown_token(store):
    assert store.revoke_session_by_token("never-issued") is False


def test_session_expires_past_its_absolute_lifetime(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    raw = store.create_session()

    store._now = lambda: now + SESSION_MAX_LIFETIME + 1
    assert store.validate_session(raw) is None


def test_session_expires_after_idle_timeout_even_within_its_absolute_lifetime(mem):
    """A session that's technically still within its 30-day window but hasn't been used in
    7 days is treated as expired too -- a stolen-but-unused cookie doesn't stay live."""
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    raw = store.create_session()
    assert SESSION_IDLE_TIMEOUT < SESSION_MAX_LIFETIME   # sanity: idle is the tighter bound

    store._now = lambda: now + SESSION_IDLE_TIMEOUT + 1
    assert store.validate_session(raw) is None


# ================================================================= device credentials (0e)
# The laptop voice client is a headless Python process -- no browser, no WebAuthn ceremony
# possible -- so it authenticates with a long-lived, explicitly-issued, revocable device
# credential instead, replacing the old shared AGENT_WS_TOKEN.
def test_issue_device_credential_survives_far_past_the_browser_idle_timeout(mem):
    from core.auth import DEVICE_MAX_LIFETIME, SESSION_IDLE_TIMEOUT

    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    token = store.issue_device_credential("Laptop")

    store._now = lambda: now + SESSION_IDLE_TIMEOUT * 3   # an always-on client gone quiet
    assert store.validate_session(token) is not None
    assert DEVICE_MAX_LIFETIME > SESSION_IDLE_TIMEOUT * 3   # sanity: still within its lifetime


def test_device_credential_still_expires_past_its_own_year_long_lifetime(mem):
    from core.auth import DEVICE_MAX_LIFETIME

    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    token = store.issue_device_credential("Laptop")

    store._now = lambda: now + DEVICE_MAX_LIFETIME + 1
    assert store.validate_session(token) is None


def test_device_credential_is_revocable_exactly_like_a_browser_session(mem):
    store = AuthStore(memory=mem, clock=lambda: 1_000_000.0)
    token = store.issue_device_credential("Laptop")
    assert store.revoke_session_by_token(token) is True
    assert store.validate_session(token) is None


def test_a_browser_session_still_enforces_its_own_idle_timeout_unaffected_by_device_logic(mem):
    """The is_device flag must not accidentally loosen the ordinary browser session path."""
    from core.auth import SESSION_IDLE_TIMEOUT

    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    raw = store.create_session()   # device=False (default)

    store._now = lambda: now + SESSION_IDLE_TIMEOUT + 1
    assert store.validate_session(raw) is None


def test_device_credential_token_is_stored_hashed_never_raw(mem):
    store = AuthStore(memory=mem, clock=lambda: 1_000_000.0)
    token = store.issue_device_credential("Laptop")
    row = mem.execute(
        "SELECT session_token_hash, is_device FROM auth_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["session_token_hash"] != token
    assert row["is_device"] == 1


def test_validating_a_session_slides_last_seen_at_forward(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    raw = store.create_session()

    later = now + 3600
    store._now = lambda: later
    store.validate_session(raw)

    row = mem.execute("SELECT last_seen_at FROM auth_sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["last_seen_at"] == later


def test_multiple_sessions_can_be_live_at_once(store):
    """No single-session-per-account assumption -- laptop and phone both stay logged in."""
    raw_a = store.create_session(user_agent="laptop")
    raw_b = store.create_session(user_agent="phone")
    assert store.validate_session(raw_a) is not None
    assert store.validate_session(raw_b) is not None
    store.revoke_session(store.validate_session(raw_a)["id"])
    assert store.validate_session(raw_a) is None
    assert store.validate_session(raw_b) is not None   # revoking one leaves the other live


# ================================================================= attempts (0d wires enforcement)
def test_record_attempt_logs_kind_ok_and_hashed_ip(store, mem):
    store.record_attempt("password", False, ip="203.0.113.5")
    row = mem.execute("SELECT kind, ok, ip_hash FROM auth_attempts ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "password"
    assert row["ok"] is False
    assert row["ip_hash"] and "203.0.113.5" not in row["ip_hash"]


# ================================================================= WebAuthn passkeys (Slice 0c)
def _fake_options(challenge=b"a-challenge"):
    return types.SimpleNamespace(challenge=challenge)


def _fake_registration_result(credential_id=b"cred-1", public_key=b"pubkey-bytes", sign_count=0):
    return types.SimpleNamespace(credential_id=credential_id, credential_public_key=public_key,
                                 sign_count=sign_count)


def _fake_authentication_result(new_sign_count):
    return types.SimpleNamespace(new_sign_count=new_sign_count)


def _register(store, *, credential_id=b"cred-1", sign_count=0, generate_fn=None):
    """Full registration round trip through the real store logic with fake crypto."""
    store.start_registration(rp_id="localhost", user_name="calvin",
                             generate_fn=generate_fn or (lambda **_: _fake_options()))
    return store.verify_registration(
        {"id": "ignored", "response": {}}, expected_rp_id="localhost",
        expected_origin="https://localhost", label="Test Device",
        verify_fn=lambda **_: _fake_registration_result(credential_id=credential_id,
                                                         sign_count=sign_count))


def test_registration_stores_the_new_credential(store, mem):
    _register(store, credential_id=b"cred-1", sign_count=0)
    row = mem.execute("SELECT * FROM credentials WHERE credential_id=%s", (b"cred-1",)).fetchone()
    assert row is not None
    assert row["public_key"] == b"pubkey-bytes"
    assert row["sign_count"] == 0
    assert row["label"] == "Test Device"


def test_verify_registration_without_a_pending_challenge_fails(store):
    with pytest.raises(WebAuthnError, match="no pending registration"):
        store.verify_registration(
            {}, expected_rp_id="localhost", expected_origin="https://localhost",
            label="Test Device", verify_fn=lambda **_: _fake_registration_result())


def test_registration_challenge_is_single_use(store):
    store.start_registration(rp_id="localhost", user_name="calvin",
                             generate_fn=lambda **_: _fake_options())
    store.verify_registration(
        {}, expected_rp_id="localhost", expected_origin="https://localhost", label="First",
        verify_fn=lambda **_: _fake_registration_result(credential_id=b"cred-1"))

    with pytest.raises(WebAuthnError, match="no pending registration"):
        store.verify_registration(
            {}, expected_rp_id="localhost", expected_origin="https://localhost", label="Second",
            verify_fn=lambda **_: _fake_registration_result(credential_id=b"cred-2"))


def test_registration_challenge_expires(mem):
    from core.auth import WEBAUTHN_CHALLENGE_TTL

    now = [1_000_000.0]
    store = AuthStore(memory=mem, clock=lambda: now[0])
    store.start_registration(rp_id="localhost", user_name="calvin",
                             generate_fn=lambda **_: _fake_options())

    now[0] += WEBAUTHN_CHALLENGE_TTL + 1
    with pytest.raises(WebAuthnError, match="no pending registration"):
        store.verify_registration(
            {}, expected_rp_id="localhost", expected_origin="https://localhost", label="Late",
            verify_fn=lambda **_: _fake_registration_result())


def test_registration_verification_failure_raises_webauthn_error(store):
    store.start_registration(rp_id="localhost", user_name="calvin",
                             generate_fn=lambda **_: _fake_options())

    def _boom(**_):
        raise RuntimeError("bad attestation")

    with pytest.raises(WebAuthnError, match="registration verification failed"):
        store.verify_registration(
            {}, expected_rp_id="localhost", expected_origin="https://localhost",
            label="Bad", verify_fn=_boom)


def test_login_options_refuse_when_no_passkey_is_registered(store):
    with pytest.raises(WebAuthnError, match="no passkey registered"):
        store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())


def test_login_with_an_unknown_credential_id_is_rejected(store):
    _register(store, credential_id=b"cred-1")
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    with pytest.raises(WebAuthnError, match="unknown or revoked credential"):
        store.verify_login(
            {}, b"never-registered", expected_rp_id="localhost",
            expected_origin="https://localhost",
            verify_fn=lambda **_: _fake_authentication_result(new_sign_count=1))


def test_login_succeeds_and_bumps_the_stored_sign_count(store, mem):
    _register(store, credential_id=b"cred-1", sign_count=5)
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    token = store.verify_login(
        {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
        verify_fn=lambda **_: _fake_authentication_result(new_sign_count=6))

    assert token   # a real session was minted
    assert store.validate_session(token) is not None
    row = mem.execute("SELECT sign_count FROM credentials WHERE credential_id=%s",
                      (b"cred-1",)).fetchone()
    assert row["sign_count"] == 6


# The one guardrail this whole slice exists to add: "a decreasing counter means a cloned
# authenticator -- reject" (the brief's own words).
def test_login_rejects_a_sign_count_that_went_backwards(store, mem):
    _register(store, credential_id=b"cred-1", sign_count=10)
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    with pytest.raises(WebAuthnError, match="sign count went backwards"):
        store.verify_login(
            {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
            verify_fn=lambda **_: _fake_authentication_result(new_sign_count=3))

    # The stored count must NOT have been corrupted by the rejected attempt.
    row = mem.execute("SELECT sign_count FROM credentials WHERE credential_id=%s",
                      (b"cred-1",)).fetchone()
    assert row["sign_count"] == 10


def test_login_does_not_flag_a_synced_passkey_stuck_at_zero(store):
    """iCloud Keychain / Google Password Manager synced passkeys legitimately report
    sign_count=0 forever -- the WebAuthn spec's own guidance is that this must not be
    treated as a regression, only a count that was once meaningful going backwards."""
    _register(store, credential_id=b"cred-1", sign_count=0)
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    token = store.verify_login(
        {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
        verify_fn=lambda **_: _fake_authentication_result(new_sign_count=0))

    assert token


def test_revoked_credential_cannot_log_in(store):
    """A second active credential keeps start_login() from refusing outright ("no passkey
    registered") so this actually exercises verify_login()'s own revoked-credential check,
    not just the emptier "nothing registered at all" case above."""
    _register(store, credential_id=b"cred-1", sign_count=0)
    _register(store, credential_id=b"cred-2", sign_count=0)
    store.revoke_credential(b"cred-1")
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    with pytest.raises(WebAuthnError, match="unknown or revoked credential"):
        store.verify_login(
            {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
            verify_fn=lambda **_: _fake_authentication_result(new_sign_count=1))


def test_a_revoked_credential_is_excluded_from_active_credentials(store):
    _register(store, credential_id=b"cred-1")
    _register(store, credential_id=b"cred-2")
    store.revoke_credential(b"cred-1")

    active = {c["credential_id"] for c in store.active_credentials()}
    assert active == {b"cred-2"}


def test_step_up_verifies_without_minting_a_new_session(store):
    """The difference from verify_login(): proves presence, doesn't log in again."""
    _register(store, credential_id=b"cred-1", sign_count=5)
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    result = store.verify_step_up(
        {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
        verify_fn=lambda **_: _fake_authentication_result(new_sign_count=6))

    assert result is True


def test_step_up_also_rejects_a_sign_count_regression(store, mem):
    """Same underlying check as login -- a stolen/cloned authenticator shouldn't be able to
    pass a high-tier step-up just because it's a different code path."""
    _register(store, credential_id=b"cred-1", sign_count=10)
    store.start_login(rp_id="localhost", generate_fn=lambda **_: _fake_options())

    with pytest.raises(WebAuthnError, match="sign count went backwards"):
        store.verify_step_up(
            {}, b"cred-1", expected_rp_id="localhost", expected_origin="https://localhost",
            verify_fn=lambda **_: _fake_authentication_result(new_sign_count=1))


# ================================================================= recovery codes (Slice 0d)
def test_recovery_codes_are_stored_argon2_hashed_never_raw(store, mem):
    codes = store.generate_recovery_codes(3)
    assert len(codes) == 3
    rows = mem.execute("SELECT code_hash FROM recovery_codes").fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["code_hash"] not in codes
        assert row["code_hash"].startswith("$argon2")


def test_a_generated_recovery_code_logs_in_and_is_then_single_use(store):
    codes = store.generate_recovery_codes(1)
    code = codes[0]

    assert store.verify_recovery_code(code) is True
    assert store.verify_recovery_code(code) is False, "a recovery code must be single-use"


def test_an_unknown_recovery_code_is_rejected(store):
    store.generate_recovery_codes(1)
    assert store.verify_recovery_code("not-a-real-code") is False


def test_generating_more_codes_does_not_invalidate_the_old_unused_set(store):
    first_batch = store.generate_recovery_codes(2)
    second_batch = store.generate_recovery_codes(2)
    assert store.unused_recovery_code_count() == 4
    assert store.verify_recovery_code(first_batch[0]) is True
    assert store.verify_recovery_code(second_batch[0]) is True


def test_unused_recovery_code_count_reflects_redemptions(store):
    codes = store.generate_recovery_codes(3)
    assert store.unused_recovery_code_count() == 3
    store.verify_recovery_code(codes[0])
    assert store.unused_recovery_code_count() == 2


# ================================================================= lockout (Slice 0d)
def test_no_attempts_means_no_lockout(store):
    locked, retry_after = store.lockout_status("password", "203.0.113.5")
    assert locked is False
    assert retry_after == 0.0


def test_a_single_failure_triggers_a_short_backoff_not_a_full_lockout(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    store.record_attempt("password", False, ip="203.0.113.5")

    locked, retry_after = store.lockout_status("password", "203.0.113.5")
    assert locked is True
    assert 0 < retry_after <= 2   # 2**1 = 2s backoff, well short of the 15-min lockout


def test_backoff_clears_once_its_own_short_window_passes(mem):
    now = [1_000_000.0]
    store = AuthStore(memory=mem, clock=lambda: now[0])
    store.record_attempt("password", False, ip="203.0.113.5")

    now[0] += 3   # past the 2s backoff from a single failure
    locked, _ = store.lockout_status("password", "203.0.113.5")
    assert locked is False


def test_reaching_the_threshold_triggers_a_full_lockout(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(5):   # password's threshold
        store.record_attempt("password", False, ip="203.0.113.5")

    locked, retry_after = store.lockout_status("password", "203.0.113.5")
    assert locked is True
    assert retry_after == pytest.approx(900, abs=1)   # the full 15-min window


def test_lockout_auto_clears_once_the_window_rolls_off(mem):
    now = [1_000_000.0]
    store = AuthStore(memory=mem, clock=lambda: now[0])
    for _ in range(5):
        store.record_attempt("password", False, ip="203.0.113.5")
    assert store.lockout_status("password", "203.0.113.5")[0] is True

    now[0] += 901   # past the 900s window
    locked, _ = store.lockout_status("password", "203.0.113.5")
    assert locked is False, "lockout must auto-clear -- never a permanent hard-fail"


def test_a_success_resets_the_failure_streak(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(4):   # one short of the 5-failure threshold
        store.record_attempt("password", False, ip="203.0.113.5")
    store.record_attempt("password", True, ip="203.0.113.5")

    locked, _ = store.lockout_status("password", "203.0.113.5")
    assert locked is False


def test_lockout_is_scoped_per_ip_not_global(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(5):
        store.record_attempt("password", False, ip="203.0.113.5")

    assert store.lockout_status("password", "203.0.113.5")[0] is True
    assert store.lockout_status("password", "198.51.100.9")[0] is False


def test_lockout_is_scoped_per_kind_not_shared_across_password_and_recovery(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(5):
        store.record_attempt("password", False, ip="203.0.113.5")

    assert store.lockout_status("password", "203.0.113.5")[0] is True
    assert store.lockout_status("recovery", "203.0.113.5")[0] is False


def test_recovery_locks_out_faster_than_password_matching_its_weaker_factor(mem):
    """The spec's own ask: password and recovery paths are stricter than passkey."""
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(3):   # recovery's threshold -- lower than password's 5
        store.record_attempt("recovery", False, ip="203.0.113.5")

    assert store.lockout_status("recovery", "203.0.113.5")[0] is True


# ================================================================= password reset OTP (Phase 36)
def test_reset_otp_is_stored_argon2_hashed_never_raw(store, mem):
    code = store.request_password_reset_otp()
    row = mem.execute(
        "SELECT code_hash FROM password_reset_otp ORDER BY id DESC LIMIT 1").fetchone()
    assert row["code_hash"] != code
    assert row["code_hash"].startswith("$argon2")
    assert len(code) == 6 and code.isdigit()


def test_reset_otp_verifies_once_then_is_rejected(store):
    code = store.request_password_reset_otp()
    assert store.verify_password_reset_otp(code) is True
    assert store.verify_password_reset_otp(code) is False, "an OTP must be single-use"


def test_an_unknown_reset_otp_is_rejected(store):
    store.request_password_reset_otp()
    assert store.verify_password_reset_otp("000000") is False


def test_requesting_a_new_reset_otp_invalidates_the_previous_unused_one(store):
    first = store.request_password_reset_otp()
    second = store.request_password_reset_otp()
    assert store.verify_password_reset_otp(first) is False, \
        "only the newest emailed code should ever work"
    assert store.verify_password_reset_otp(second) is True


def test_reset_otp_expires(mem):
    now = [1_000_000.0]
    store = AuthStore(memory=mem, clock=lambda: now[0])
    code = store.request_password_reset_otp()
    now[0] += 601   # past the 10-minute TTL
    assert store.verify_password_reset_otp(code) is False


def test_reset_otp_lockout_uses_its_own_kind_not_passwords(mem):
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(5):
        store.record_attempt("password_reset_otp", False, ip="")
    assert store.lockout_status("password_reset_otp", "")[0] is True
    assert store.lockout_status("password", "")[0] is False


# ================================================================= revoke_all_sessions
def test_revoke_all_sessions_keeps_devices_by_default(store, mem):
    browser = store.create_session(user_agent="chrome")
    device = store.issue_device_credential("Kelvin Laptop")

    revoked = store.revoke_all_sessions()

    assert revoked == 1
    assert store.validate_session(browser) is None
    assert store.validate_session(device) is not None


def test_revoke_all_sessions_can_include_devices_too(store):
    browser = store.create_session(user_agent="chrome")
    device = store.issue_device_credential("Kelvin Laptop")

    revoked = store.revoke_all_sessions(keep_devices=False)

    assert revoked == 2
    assert store.validate_session(browser) is None
    assert store.validate_session(device) is None


def test_revoke_all_sessions_is_a_safe_no_op_with_nothing_active(store):
    assert store.revoke_all_sessions() == 0


def test_checking_lockout_status_never_itself_counts_as_an_attempt(mem):
    """Otherwise polling while locked out would extend the lockout indefinitely -- the
    opposite of "auto-clears"."""
    now = 1_000_000.0
    store = AuthStore(memory=mem, clock=lambda: now)
    for _ in range(5):
        store.record_attempt("password", False, ip="203.0.113.5")

    for _ in range(10):
        store.lockout_status("password", "203.0.113.5")

    row = mem.execute(
        "SELECT COUNT(*) c FROM auth_attempts WHERE kind='password'").fetchone()
    assert row["c"] == 5


# ================================================================= AGENT_WS_TOKEN is gone (0e)
# The spec's own closing guardrail: "No code path reads AGENT_WS_TOKEN after migration."
# A regex over the string alone would false-positive on every explanatory comment in this
# diff ("...replaces the old AGENT_WS_TOKEN...") — the meaningful check is that nothing
# actually CALLS getenv/environ for it anymore.
_AUTH_SCAN_DIRS = ["core", "skills", "kernel", "client"]
_GETENV_AGENT_WS_TOKEN = re.compile(
    r"""(?:os\.)?(?:getenv|environ(?:\.get)?)\s*[\(\[]\s*["']AGENT_WS_TOKEN["']""")


def _auth_scan_python_files():
    project = Path(__file__).resolve().parent.parent
    for d in _AUTH_SCAN_DIRS:
        assert (project / d).is_dir(), f"scan target '{d}/' is missing — coverage is a lie"
        yield from (project / d).rglob("*.py")
    yield project / "manage.py"


def test_no_code_path_reads_agent_ws_token():
    offenders = []
    for path in _auth_scan_python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _GETENV_AGENT_WS_TOKEN.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, f"AGENT_WS_TOKEN is still read (must be fully migrated): {offenders}"


def test_agent_ws_token_scan_still_catches_a_real_offender():
    """The scan itself must not be a no-op."""
    assert _GETENV_AGENT_WS_TOKEN.search('os.getenv("AGENT_WS_TOKEN", "")')
    assert _GETENV_AGENT_WS_TOKEN.search('os.environ["AGENT_WS_TOKEN"]')
    # a prose comment mentioning the name must NOT trip the scan
    assert not _GETENV_AGENT_WS_TOKEN.search(
        "# Slice 0e: AGENT_WS_TOKEN is gone. Use AGENT_DEVICE_TOKEN instead.")


def test_settings_no_longer_exposes_ws_token():
    """The structural half: no ws_token-shaped field survives on Settings at all."""
    from core.config import Settings

    fields = set(Settings.__dataclass_fields__)
    assert "ws_token" not in fields
