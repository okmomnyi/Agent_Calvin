"""Voice control skill (Phase 7).

Manages which pre-built neural voice AgentOS speaks with and the speaking rate. The voice
registry is FIXED to Microsoft edge-tts stock voices declared in config.yaml (Guy, Aria,
Ryan, Rafiki, Zuri). Per §0 Principle 9 there is intentionally NO path here to record,
train, fine-tune, or synthesize a voice from Calvin's own speech — "change voice to X"
can only ever select from the stock registry, and a request for anything else is refused.
"""

from __future__ import annotations

from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.voice")

_KV_VOICE = "voice.current"
_KV_RATE = "voice.rate"          # integer percent, e.g. -10, 0, +15
_KV_MUTED = "voice.muted"        # "1" when TTS is muted (/voiceoff)
_RATE_STEP = 10
_RATE_MIN, _RATE_MAX = -50, 50


class VoiceSkill(BaseSkill):
    name = "voice"

    def __init__(self, memory: Memory | None = None) -> None:
        self._mem = memory

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "set_voice": self.set_voice,
            "get_voice": self.get_voice,
            "list_voices": self.list_voices,
            "set_rate": self.set_rate,
            "mute": lambda **_: self.set_muted(True),
            "unmute": lambda **_: self.set_muted(False),
        }

    def contract(self) -> SkillContract:
        """No instruction can ever add a cloned voice — the registry is stock voices only."""
        return SkillContract(reads_categories=["tone", "notifications"],
                             hard_invariants=["prebuilt_voices_only"])

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- registry (pre-built only)
    @property
    def registry(self) -> dict[str, str]:
        """alias -> edge-tts neural voice id. Fixed stock voices only (§0 Principle 9)."""
        return dict(get_settings().get("voice", "registry", default={}) or {})

    @property
    def default_alias(self) -> str:
        return get_settings().get("voice", "default", default="guy")

    # ------------------------------------------------------------- current state
    def current(self) -> dict[str, Any]:
        """Return the active voice alias, its edge-tts id, and the rate string for edge-tts."""
        alias = self.mem.kv_get(_KV_VOICE) or self.default_alias
        registry = self.registry
        if alias not in registry:  # config changed / stale kv — fall back safely
            alias = self.default_alias if self.default_alias in registry else (
                next(iter(registry), "guy"))
        rate = self._rate_int()
        return {"voice": alias, "voice_id": registry.get(alias, ""), "rate_percent": rate,
                "rate": _fmt_rate(rate), "muted": self.is_muted()}

    def is_muted(self) -> bool:
        return self.mem.kv_get(_KV_MUTED) == "1"

    def set_muted(self, muted: bool) -> CommandResult:
        self.mem.kv_set(_KV_MUTED, "1" if muted else "0")
        return CommandResult(text="TTS muted — I'll reply in text only." if muted
                             else "TTS on — I'll speak replies again.", data={"muted": muted})

    def _rate_int(self) -> int:
        try:
            return int(self.mem.kv_get(_KV_RATE) or "0")
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------- actions
    def set_voice(self, voice: str = "", **_: Any) -> CommandResult:
        """Switch to a registry voice by alias (e.g. 'zuri'). Rejects anything not pre-built."""
        req = (voice or "").strip().lower()
        registry = self.registry
        if not req:
            return CommandResult(text=f"Which voice? Options: {', '.join(registry)}.", ok=False)
        # accept either the alias ('zuri') or the exact stock id ('sw-KE-ZuriNeural')
        alias = req if req in registry else next(
            (a for a, vid in registry.items() if vid.lower() == req), "")
        if not alias:
            return CommandResult(
                text=(f"'{voice}' isn't in my voice registry. I only use the pre-built voices: "
                      f"{', '.join(registry)}. (I can't clone or create new voices.)"),
                ok=False, data={"rejected": voice})
        self.mem.kv_set(_KV_VOICE, alias)
        cur = self.current()
        return CommandResult(text=f"Voice set to {alias.capitalize()}.", data=cur)

    def get_voice(self, **_: Any) -> CommandResult:
        cur = self.current()
        return CommandResult(text=f"Current voice: {cur['voice'].capitalize()} "
                                  f"({cur['voice_id']}), rate {cur['rate']}.", data=cur)

    def list_voices(self, **_: Any) -> CommandResult:
        registry = self.registry
        lines = [f"- {alias}: {vid}" for alias, vid in registry.items()]
        return CommandResult(text="Available pre-built voices:\n" + "\n".join(lines),
                             data={"voices": registry})

    def set_rate(self, direction: str = "", value: int | None = None, **_: Any) -> CommandResult:
        """Adjust speaking rate. direction 'slower'/'faster' steps by 10%, or pass an explicit value."""
        rate = self._rate_int()
        if value is not None:
            rate = int(value)
        else:
            d = (direction or "").lower()
            if "slow" in d:
                rate -= _RATE_STEP
            elif "fast" in d or "quick" in d:
                rate += _RATE_STEP
            else:
                return CommandResult(text="Say 'speak slower' or 'speak faster'.", ok=False)
        rate = max(_RATE_MIN, min(_RATE_MAX, rate))
        self.mem.kv_set(_KV_RATE, str(rate))
        return CommandResult(text=f"Speaking rate now {_fmt_rate(rate)}.", data=self.current())


def _fmt_rate(percent: int) -> str:
    """edge-tts rate string, e.g. 0 -> '+0%', -10 -> '-10%'."""
    return f"{'+' if percent >= 0 else ''}{percent}%"


SKILL = VoiceSkill()
