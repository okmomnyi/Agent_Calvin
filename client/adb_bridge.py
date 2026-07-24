"""ADB bridge (Phase 36 Slice 4) — the laptop-side half of phone control.

Same trust boundary Phase 23 established for desktop app control, applied to a phone
plugged into THIS laptop: the droplet can ask for an action, but only this process decides
whether to do it and how, and it re-validates everything regardless of what the server sent.

Structural rules, not policy:

* **No pixel-coordinate automation, ever.** Every op is an Android intent or a keyevent —
  resolution- and UI-independent. A `tap(x, y)` here would be an automatic design failure.
* **Every phone number is validated against strict E.164** (`^\\+[1-9]\\d{7,14}$`) BEFORE it
  goes anywhere near a subprocess. There is no path from an unvalidated string to argv.
* **argv lists only, `shell=False` always.** Nothing is ever built by string interpolation —
  even a number that already passed validation is one argv element, never concatenated into
  a command string.
* **Device detection is explicit.** No device, an unauthorized device, or more than one
  connected device are each a clean, actionable error — never a traceback, never a silent
  guess at which phone to dial from.
* **Call-state polling is caller-driven.** `call_state()` is a single `dumpsys` read; nothing
  in this module starts its own background poll loop. skills/phone.py (Slice 5) decides when
  polling is worth the cost — only while a call is actually live.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable

# Strict E.164: a leading '+', a non-zero first digit, 8-15 digits total. Anything looser
# risks a malformed string reaching `tel:` on a real device.
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# KEYCODE_HEADSETHOOK first (works even with the screen off on most OEMs), falling back to
# KEYCODE_CALL. KEYCODE_ENDCALL for hangup. No force/kill equivalent exists for a phone call.
ANSWER_KEYCODES: tuple[int, ...] = (79, 5)
HANGUP_KEYCODE = 6

_CALL_STATES = {"0": "idle", "1": "ringing", "2": "offhook"}


def is_valid_e164(number: str) -> bool:
    return bool(E164_RE.match(number or ""))


@dataclass
class Outcome:
    ok: bool
    detail: str

    def __str__(self) -> str:  # what a caller prints/logs
        return ("" if self.ok else "! ") + self.detail


class AdbBridge:
    """Executes phone ops against a connected, authorized Android device. Never raises."""

    def __init__(self, runner: Callable[[list[str]], "subprocess.CompletedProcess[str]"]
                 | None = None) -> None:
        self._run = runner or _run

    # ------------------------------------------------------------- devices
    def devices(self) -> list[str]:
        """Serials of connected AND authorized devices. Never raises — no adb on PATH, or
        no device, both resolve to an empty list, not an exception."""
        try:
            proc = self._run(["adb", "devices"])
        except Exception:  # noqa: BLE001 - adb missing entirely is a "no device" state
            return []
        out: list[str] = []
        for line in proc.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                out.append(parts[0])
        return out

    def _select_device(self, serial: str | None) -> tuple[str | None, Outcome | None]:
        connected = self.devices()
        if not connected:
            return None, Outcome(
                False, "no Android device connected — check USB debugging is on, the "
                       "device is plugged in (or paired via `adb pair` for wireless), and "
                       "the trust prompt on the phone has been accepted")
        if serial:
            if serial not in connected:
                return None, Outcome(
                    False, f"device {serial!r} is not connected (connected: "
                           f"{', '.join(connected)})")
            return serial, None
        if len(connected) > 1:
            return None, Outcome(
                False, f"more than one device connected ({', '.join(connected)}) — "
                       f"specify which")
        return connected[0], None

    @staticmethod
    def _adb(serial: str, *args: str) -> list[str]:
        return ["adb", "-s", serial, *args]

    # ------------------------------------------------------------- ops
    def call(self, number: str, *, serial: str | None = None) -> Outcome:
        if not is_valid_e164(number):
            return Outcome(False, f"refused: {number!r} is not a valid E.164 number")
        device, error = self._select_device(serial)
        if error:
            return error
        argv = self._adb(device, "shell", "am", "start", "-a",
                         "android.intent.action.CALL", "-d", f"tel:{number}")
        return self._exec(argv, f"calling {number}")

    def answer(self, *, serial: str | None = None) -> Outcome:
        device, error = self._select_device(serial)
        if error:
            return error
        result = Outcome(False, "answering failed")
        for keycode in ANSWER_KEYCODES:
            result = self._exec(
                self._adb(device, "shell", "input", "keyevent", str(keycode)), "answering")
            if result.ok:
                return result
        return result

    def hangup(self, *, serial: str | None = None) -> Outcome:
        device, error = self._select_device(serial)
        if error:
            return error
        argv = self._adb(device, "shell", "input", "keyevent", str(HANGUP_KEYCODE))
        return self._exec(argv, "ending the call")

    def call_state(self, *, serial: str | None = None) -> str | None:
        """One-shot read of the live call state, or None if it can't be determined. The
        CALLER decides polling cadence (see module docstring) — this is a single read."""
        device, error = self._select_device(serial)
        if error:
            return None
        try:
            proc = self._run(self._adb(device, "shell", "dumpsys", "telephony.registry"))
        except Exception:  # noqa: BLE001
            return None
        for line in proc.stdout.splitlines():
            if "mCallState" in line:
                digits = "".join(c for c in line.split("=")[-1] if c.isdigit())
                return _CALL_STATES.get(digits)
        return None

    def _exec(self, argv: list[str], verb: str) -> Outcome:
        try:
            proc = self._run(argv)
        except Exception as exc:  # noqa: BLE001 - a bad ADB call must not crash the bridge
            return Outcome(False, f"{verb} failed: {exc}")
        if proc.returncode != 0:
            return Outcome(False, f"{verb} failed (exit {proc.returncode}): "
                                  f"{(proc.stderr or '').strip()}")
        return Outcome(True, verb)


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    """shell=False is the point — argv never goes through a shell."""
    return subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=15)
