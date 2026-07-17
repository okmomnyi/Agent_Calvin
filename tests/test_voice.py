"""Voice tests: voice skill (pre-built only, persistence, rate), client helpers, and the
§0 guardrail — NO voice/face-cloning code path exists anywhere in the codebase."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from client.voice_utils import (detect_local_command, format_rate, is_silent,
                                silence_elapsed, strip_wake_word)
from skills.voice import VoiceSkill


# ------------------------------------------------------------------ voice skill
def test_set_voice_persists_and_rejects_unknown(mem):
    skill = VoiceSkill(memory=mem)
    ok = skill.set_voice(voice="zuri")
    assert ok.ok and ok.data["voice"] == "zuri"
    assert ok.data["voice_id"] == "sw-KE-ZuriNeural"
    # persisted
    assert VoiceSkill(memory=mem).current()["voice"] == "zuri"

    bad = skill.set_voice(voice="my-own-cloned-voice")
    assert bad.ok is False
    assert "can't clone" in bad.text.lower() or "registry" in bad.text.lower()


def test_set_voice_accepts_exact_stock_id(mem):
    skill = VoiceSkill(memory=mem)
    res = skill.set_voice(voice="en-GB-RyanNeural")
    assert res.ok and res.data["voice"] == "ryan"


def test_rate_steps_and_clamps(mem):
    skill = VoiceSkill(memory=mem)
    assert skill.set_rate(direction="slower").data["rate_percent"] == -10
    assert skill.set_rate(direction="slower").data["rate_percent"] == -20
    for _ in range(10):
        skill.set_rate(direction="slower")
    assert skill.current()["rate_percent"] == -50  # clamped at floor


def test_current_defaults_to_registry_default(mem):
    assert VoiceSkill(memory=mem).current()["voice"] == "guy"


# ------------------------------------------------------------------ client helpers
def test_format_rate():
    assert format_rate(0) == "+0%"
    assert format_rate(-10) == "-10%"
    assert format_rate(15) == "+15%"


def test_detect_local_command():
    assert detect_local_command("stop") == "stop"
    assert detect_local_command("cancel.") == "stop"
    assert detect_local_command("check my email") is None


def test_strip_wake_word():
    assert strip_wake_word("Hey Agent, check my email") == "check my email"
    assert strip_wake_word("agent what's due") == "what's due"
    assert strip_wake_word("no wake word here") == "no wake word here"


def test_is_silent_and_elapsed():
    assert is_silent(b"\x00\x00" * 100) is True
    loud = (b"\xff\x7f" * 100)  # max amplitude samples
    assert is_silent(loud) is False
    assert silence_elapsed(40) is True     # 40 * 30ms = 1200ms
    assert silence_elapsed(10) is False


# ------------------------------------------------------------------ §0: no cloning anywhere
_BANNED = re.compile(
    r"\b(import|from)\s+(elevenlabs|TTS|coqui|so_vits_svc|so_vits|rvc|tortoise_tts|bark)\b"
    r"|\b(clone_voice|voice_clone|train_voice|fine_tune_voice|faceswap|face_swap|deepfake|"
    r"synthesize_face|face_clone)\b",
    re.I)

# Only edge-tts (pre-built neural voices) is an allowed TTS engine.
_PROJECT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ["core", "skills", "kernel", "client"]


def _python_files():
    for d in _SCAN_DIRS:
        # A missing directory would make rglob yield nothing and the guardrail below pass
        # while scanning less than it claims — which is exactly what happened the first time
        # this ran in Docker, with client/ left out of the image.
        assert (_PROJECT / d).is_dir(), f"§0 scan target '{d}/' is missing — coverage is a lie"
        yield from (_PROJECT / d).rglob("*.py")


# The Phase-20 invariant tripwires must literally contain these words in order to REFUSE
# rules that ask for them. A line that maps a pattern to a cloning invariant is enforcement
# code, not a cloning path — everything else is still banned outright.
_ENFORCEMENT_MARKERS = ("no_face_cloning", "no_voice_cloning", "prebuilt_voices_only")


def _is_enforcement_line(line: str) -> bool:
    return any(marker in line for marker in _ENFORCEMENT_MARKERS)


def test_no_voice_or_face_cloning_code_path():
    offenders = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _is_enforcement_line(line):
                continue                     # this line BLOCKS cloning; it doesn't do it
            for m in _BANNED.finditer(line):
                offenders.append(f"{path.name}:{lineno}: {m.group(0)}")
    assert not offenders, f"Cloning code path detected (forbidden by §0): {offenders}"


def test_cloning_guardrail_still_catches_a_real_offender(tmp_path):
    """The exemption above must not blunt the scan: a genuine cloning line is still caught."""
    assert _BANNED.search("model = clone_voice(sample)")
    assert _BANNED.search("import elevenlabs")
    assert not _is_enforcement_line("model = clone_voice(sample)")
    # ...while the enforcement tripwire is correctly ignored
    assert _is_enforcement_line('(r"\\b(clone|deepfake)\\b", "no_face_cloning"),')


def test_voice_registry_is_prebuilt_stock_only():
    from core.config import get_settings

    registry = get_settings().get("voice", "registry", default={})
    assert registry, "voice registry missing from config"
    # every stock edge-tts voice id ends in 'Neural' — proves these are Microsoft's pre-built voices
    for alias, vid in registry.items():
        assert vid.endswith("Neural"), f"{alias} -> {vid} is not a stock edge-tts neural voice"


def test_client_only_uses_edge_tts():
    client_src = (_PROJECT / "client" / "voice_client.py").read_text(encoding="utf-8")
    assert "edge_tts" in client_src
    assert not re.search(r"\b(elevenlabs|coqui|so_vits|rvc|tortoise)\b", client_src, re.I)


# ============================================================ no test may text a human
def test_no_skill_sends_telegram_without_an_injection_point():
    """Every skill that pushes to Telegram must accept an injectable `notify`.

    Three did not (job_hunter, semester_planner, lecture_capture) while calling send_telegram
    unconditionally, so every full-suite run fired real messages at Calvin's phone: an
    interview invite from a fixture's hr@acme.com, a lecture he never recorded, a deadline
    that did not exist. It ran all night before he pasted the chat log.

    conftest's autouse guard severs the transport, but that only turns a text into a failure.
    This asserts the design property: if you can reach a human, a test can replace you.
    """
    import inspect
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "skills"
    offenders = []
    for py in list(root.glob("*.py")) + list(root.glob("*/skill.py")):
        src = py.read_text(encoding="utf-8")
        # calls send_telegram(...) directly rather than through an injected self._notify
        import re
        if not re.search(r"(?<![\w.])send_telegram\(", src):
            continue
        mod_name = ("skills." + py.stem if py.parent == root
                    else f"skills.{py.parent.name}.skill")
        import importlib
        mod = importlib.import_module(mod_name)
        skill = getattr(mod, "SKILL", None)
        if skill is None:
            continue
        if "notify" not in inspect.signature(type(skill).__init__).parameters:
            offenders.append(f"{mod_name} calls send_telegram() but takes no `notify` kwarg")
    assert not offenders, "skills that can text Calvin with no way to inject a fake:\n  " + \
                          "\n  ".join(offenders)
