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
        return {"reply": self.reply}

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


SKILL = ChatSkill()
