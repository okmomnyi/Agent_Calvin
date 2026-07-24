"""ADB bridge (Phase 36 Slice 4) — the laptop-side half of phone control.

No real ADB, no real device: every subprocess call is injected. The properties that matter:
a malformed or non-E.164 number NEVER reaches subprocess, every call is `shell=False` argv
(no string interpolation), "no device" / "wrong device" / "ambiguous device" are each a
clean Outcome rather than a traceback, and nothing here ever raises into a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from client.adb_bridge import AdbBridge, is_valid_e164

VALID_NUMBER = "+254712345678"


@dataclass
class _FakeProc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class _FakeRunner:
    """Records every argv it was asked to run; scripted to return canned output per-command
    or raise, so a test can prove exactly what would have hit a real shell."""

    def __init__(self, devices_output: str = "", responses: dict | None = None,
                 raise_on: tuple[str, ...] = ()) -> None:
        self.devices_output = devices_output
        self.responses = responses or {}
        self.raise_on = raise_on
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> _FakeProc:
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle in self.raise_on:
            if needle in joined:
                raise RuntimeError(f"adb exploded on: {needle}")
        if argv[:2] == ["adb", "devices"]:
            return _FakeProc(stdout=self.devices_output)
        for needle, proc in self.responses.items():
            if needle in joined:
                return proc
        return _FakeProc(returncode=0)


ONE_DEVICE = "List of devices attached\nABCDE12345\tdevice\n"
TWO_DEVICES = "List of devices attached\nABCDE12345\tdevice\nFGHIJ67890\tdevice\n"
UNAUTHORIZED = "List of devices attached\nABCDE12345\tunauthorized\n"
NO_DEVICES = "List of devices attached\n"


# ================================================================= E.164 validation
@pytest.mark.parametrize("number", [
    "+254712345678", "+15551234567", "+447911123456",
])
def test_valid_e164_numbers_pass(number):
    assert is_valid_e164(number) is True


@pytest.mark.parametrize("number", [
    "0712345678",          # not E.164 (no country code)
    "254712345678",        # missing '+'
    "+0712345678",         # leading zero after '+'
    "+254 712 345 678",    # spaces
    "+254-712-345-678",    # dashes
    "'; rm -rf / #",        # injection attempt
    "+254712345678; ls",   # injection attempt
    "",
    "not a number",
])
def test_invalid_numbers_are_rejected(number):
    assert is_valid_e164(number) is False


# ================================================================= call — number never reaches subprocess
def test_a_malformed_number_never_reaches_subprocess():
    runner = _FakeRunner(devices_output=ONE_DEVICE)
    bridge = AdbBridge(runner=runner)
    result = bridge.call("not-a-number")
    assert result.ok is False
    assert runner.calls == [], "an invalid number must never trigger any subprocess call"


def test_an_injection_attempt_never_reaches_subprocess():
    runner = _FakeRunner(devices_output=ONE_DEVICE)
    bridge = AdbBridge(runner=runner)
    result = bridge.call("+254712345678; rm -rf /")
    assert result.ok is False
    assert runner.calls == []


def test_a_valid_call_builds_argv_never_a_shell_string():
    runner = _FakeRunner(devices_output=ONE_DEVICE)
    bridge = AdbBridge(runner=runner)
    result = bridge.call(VALID_NUMBER)
    assert result.ok is True
    call_argv = runner.calls[-1]
    assert call_argv == [
        "adb", "-s", "ABCDE12345", "shell", "am", "start", "-a",
        "android.intent.action.CALL", "-d", f"tel:{VALID_NUMBER}",
    ]
    # Every element is a separate argv item -- nothing is a pre-joined shell string.
    assert all(isinstance(part, str) and ";" not in part for part in call_argv)


# ================================================================= device selection
def test_no_device_is_a_clean_error_not_a_crash():
    bridge = AdbBridge(runner=_FakeRunner(devices_output=NO_DEVICES))
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False
    assert "no android device" in result.detail.lower()


def test_unauthorized_device_counts_as_no_device():
    bridge = AdbBridge(runner=_FakeRunner(devices_output=UNAUTHORIZED))
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False


def test_multiple_devices_without_a_serial_asks_rather_than_guessing():
    bridge = AdbBridge(runner=_FakeRunner(devices_output=TWO_DEVICES))
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False
    assert "ABCDE12345" in result.detail and "FGHIJ67890" in result.detail


def test_multiple_devices_with_an_explicit_serial_works():
    runner = _FakeRunner(devices_output=TWO_DEVICES)
    bridge = AdbBridge(runner=runner)
    result = bridge.call(VALID_NUMBER, serial="FGHIJ67890")
    assert result.ok is True
    assert runner.calls[-1][:3] == ["adb", "-s", "FGHIJ67890"]


def test_an_adb_binary_that_is_missing_entirely_is_a_clean_no_device_state():
    def raiser(argv):
        raise FileNotFoundError("adb not found")

    bridge = AdbBridge(runner=raiser)
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False
    assert "no android device" in result.detail.lower()


# ================================================================= answer / hangup
def test_answer_tries_headsethook_first_then_falls_back_to_call_keycode():
    runner = _FakeRunner(devices_output=ONE_DEVICE, raise_on=("keyevent 79",))
    bridge = AdbBridge(runner=runner)
    result = bridge.answer()
    assert result.ok is True
    keyevent_calls = [c for c in runner.calls if "keyevent" in c]
    assert "79" in keyevent_calls[0]
    assert "5" in keyevent_calls[1]


def test_answer_succeeds_on_first_keycode_without_trying_the_second():
    runner = _FakeRunner(devices_output=ONE_DEVICE)
    bridge = AdbBridge(runner=runner)
    bridge.answer()
    keyevent_calls = [c for c in runner.calls if "keyevent" in c]
    assert len(keyevent_calls) == 1
    assert "79" in keyevent_calls[0]


def test_hangup_sends_endcall_keycode():
    runner = _FakeRunner(devices_output=ONE_DEVICE)
    bridge = AdbBridge(runner=runner)
    result = bridge.hangup()
    assert result.ok is True
    assert "6" in runner.calls[-1]
    assert "keyevent" in runner.calls[-1]


def test_no_force_kill_path_exists():
    """There is no keycode/op anywhere in this module for a force-terminate -- only the
    graceful ENDCALL keyevent. Asserting the module's public surface has nothing else."""
    public = {name for name in dir(AdbBridge) if not name.startswith("_")}
    assert public == {"devices", "call", "answer", "hangup", "call_state"}


# ================================================================= call state
def test_call_state_parses_ringing():
    runner = _FakeRunner(devices_output=ONE_DEVICE,
                         responses={"telephony.registry": _FakeProc(
                             stdout="  mCallState=1\n  other=stuff\n")})
    bridge = AdbBridge(runner=runner)
    assert bridge.call_state() == "ringing"


def test_call_state_parses_offhook():
    runner = _FakeRunner(devices_output=ONE_DEVICE,
                         responses={"telephony.registry": _FakeProc(stdout="mCallState=2")})
    bridge = AdbBridge(runner=runner)
    assert bridge.call_state() == "offhook"


def test_call_state_with_no_device_is_none_not_an_exception():
    bridge = AdbBridge(runner=_FakeRunner(devices_output=NO_DEVICES))
    assert bridge.call_state() is None


def test_call_state_with_unparseable_output_is_none():
    runner = _FakeRunner(devices_output=ONE_DEVICE,
                         responses={"telephony.registry": _FakeProc(stdout="garbage output")})
    bridge = AdbBridge(runner=runner)
    assert bridge.call_state() is None


# ================================================================= never raises
def test_a_runner_exception_never_propagates_out_of_call():
    def raiser(argv):
        if argv[:2] == ["adb", "devices"]:
            return _FakeProc(stdout=ONE_DEVICE)
        raise TimeoutError("adb hung")

    bridge = AdbBridge(runner=raiser)
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False
    assert "failed" in result.detail.lower()


def test_a_nonzero_exit_code_is_reported_not_silently_ok():
    runner = _FakeRunner(devices_output=ONE_DEVICE,
                         responses={"am start": _FakeProc(returncode=1, stderr="boom")})
    bridge = AdbBridge(runner=runner)
    result = bridge.call(VALID_NUMBER)
    assert result.ok is False
    assert "boom" in result.detail
