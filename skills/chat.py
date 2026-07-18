"""Chit-chat / fallback skill.

Handles the `chat` intent (and anything the router could not route elsewhere) with a
short, spoken-friendly reply via the voice_chat task class. Serves as the reference
implementation of the Skill interface and guarantees the registry always finds at
least one skill during Phase 1.
"""

from __future__ import annotations

from typing import Any, Callable

from core.llm import LLMClient, get_client
from core.skill import BaseSkill, CommandResult, ScheduledJob


class ChatSkill(BaseSkill):
    name = "chat"

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"reply": self.reply, "time_status": self.time_status}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    def reply(self, text: str = "", **_: Any) -> CommandResult:
        """Answer a general conversational message concisely."""
        if not text.strip():
            return CommandResult(text="I'm here — what do you need?")
        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentOS, Calvin's personal assistant. Reply briefly and "
                    "naturally, suitable for reading aloud. No markdown."
                ),
            },
            {"role": "user", "content": text},
        ]
        reply = self.llm.chat("voice_chat", messages, max_tokens=200)
        return CommandResult(text=reply)

    def time_status(self, **_: Any) -> CommandResult:
        """Return authoritative local time without asking a model to guess or classify it."""
        from core.config import get_settings
        from core.time_context import local_now

        now = local_now()
        return CommandResult(
            text=(f"It is {now.strftime('%H:%M')} on {now.strftime('%A, %d %B %Y')} "
                  f"in {get_settings().tz} ({now.tzname()})."),
            data={"local_time": now.isoformat(), "timezone": get_settings().tz},
        )


SKILL = ChatSkill()
