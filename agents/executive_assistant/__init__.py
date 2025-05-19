from __future__ import annotations

# mypy: ignore-errors

"""Executive Assistant agent.

This agent orchestrates other agents and provides voice capabilities using
Deepgram STT and TTS models. It maintains short-term and long-term memory and
can retrieve information via a simple RAG component.
"""
from agents import Agent  # noqa: E402

from .memory import LongTermMemory, ShortTermMemory  # noqa: E402
from .rag import Retriever  # noqa: E402
from .tools import get_calendar_events, send_email  # noqa: E402


class ExecutiveAssistantState:
    """Holds resources used by the Executive Assistant."""

    def __init__(self, memory_path: str = "memory.json") -> None:
        self.short_memory = ShortTermMemory()
        self.long_memory = LongTermMemory(memory_path)
        self.retriever = Retriever()


executive_assistant_agent = Agent(
    name="ExecutiveAssistant",
    instructions=(
        "You are an executive assistant. Use the available tools to help the user. "
        "Remember important facts during the conversation for later retrieval."
    ),
    model="gpt-4o-mini",
    tools=[get_calendar_events, send_email],
)

__all__ = ["ExecutiveAssistantState", "executive_assistant_agent"]
