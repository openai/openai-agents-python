"""
Streaming lifecycle events.

This example demonstrates how to use ``Runner.run_streamed()`` with
``stream_events()`` to observe every lifecycle event in an agent run —
not just the final text output, but the full intermediate event log:
when the agent starts thinking, when a tool is called, when the tool
result comes back, and when text chunks are generated.

This is distinct from the existing streaming examples (``stream_text.py``,
``stream_items.py``) that focus on extracting a single kind of output.
Here the goal is to give a clear, structured view of *all* event types
the SDK emits so you can build your own monitoring, logging, or UI layer
on top of streamed runs.

Three event types are handled:

- ``AgentUpdatedStreamEvent`` — fires once when an agent turn starts.
- ``RawResponsesStreamEvent`` — low-level LLM deltas; we surface text
  chunks here.
- ``RunItemStreamEvent`` — higher-level items: tool calls and tool
  results.

Run with:
    OPENAI_API_KEY=sk-... uv run python examples/streaming_lifecycle_events.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from openai.types.responses import ResponseTextDeltaEvent

from agents import Agent, Runner, function_tool, trace
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)

# ---------------------------------------------------------------------------
# Mock tool definitions
# ---------------------------------------------------------------------------


@function_tool
def get_weather(city: str) -> str:
    """Return mock current weather for the given city.

    Args:
        city: The name of the city to get weather for.

    Returns:
        A plain-text description of the current weather conditions.
    """
    mock_data: dict[str, str] = {
        "tokyo": "Partly cloudy, 22 C, humidity 65%, light easterly breeze.",
        "london": "Overcast with light drizzle, 14 C, humidity 82%, calm winds.",
        "paris": "Mostly sunny, 19 C, humidity 55%, gentle south-westerly wind.",
        "new york": "Clear skies, 25 C, humidity 48%, moderate north wind.",
        "sydney": "Sunny intervals, 18 C, humidity 70%, fresh sea breeze.",
    }
    return mock_data.get(
        city.lower(), f"Mild and clear, 20 C, humidity 60%. (mock data for {city})"
    )


@function_tool
def get_time(timezone: str) -> str:
    """Return mock current local time for the given timezone abbreviation.

    Args:
        timezone: A timezone abbreviation such as ``"JST"``, ``"GMT"``, or
            ``"EST"``.

    Returns:
        A plain-text string with the mock local time in that timezone.
    """
    mock_data: dict[str, str] = {
        "JST": "15:42 JST (Japan Standard Time, UTC+9)",
        "GMT": "06:42 GMT (Greenwich Mean Time, UTC+0)",
        "BST": "07:42 BST (British Summer Time, UTC+1)",
        "CET": "08:42 CET (Central European Time, UTC+1)",
        "EST": "01:42 EST (Eastern Standard Time, UTC-5)",
        "PST": "22:42 PST (Pacific Standard Time, UTC-8)",
    }
    return mock_data.get(
        timezone.upper(),
        f"06:42 UTC (mock time for timezone {timezone})",
    )


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

weather_agent = Agent(
    name="WeatherAgent",
    model="gpt-4o-mini",
    tools=[get_weather, get_time],
    instructions=("Help users with weather and time queries. Use tools when needed."),
)


# ---------------------------------------------------------------------------
# Event logger
# ---------------------------------------------------------------------------


@dataclass
class EventLogger:
    """Collect and display structured lifecycle events from a streamed run.

    Attributes:
        events: Ordered list of formatted log lines accumulated during the run.
        tool_calls: Running count of tool-call items observed.
        text_chunks: Running count of text-delta chunks observed.
    """

    events: list[str] = field(default_factory=list)
    tool_calls: int = 0
    text_chunks: int = 0

    def log(self, event_type: str, detail: str) -> None:
        """Append a formatted line and print it with an emoji prefix.

        Args:
            event_type: Short label for the event category (e.g. ``"AGENT"``).
            detail: Human-readable description of this specific event.
        """
        emoji_map: dict[str, str] = {
            "AGENT": "🤖",
            "TEXT": "💬",
            "TOOL_CALL": "🔧",
            "TOOL_RESULT": "✅",
        }
        prefix = emoji_map.get(event_type, "📌")
        line = f"[{event_type}] {detail}"
        self.events.append(line)
        print(f"  {prefix} {line}")

    def summary(self) -> None:
        """Print totals for events, tool calls, and text chunks."""
        print(
            f"\n  --- Summary: {len(self.events)} events logged, "
            f"{self.tool_calls} tool call(s), "
            f"{self.text_chunks} text chunk(s) ---"
        )


# ---------------------------------------------------------------------------
# Streamed run with full event logging
# ---------------------------------------------------------------------------


async def run_with_event_logging(query: str) -> None:
    """Run the weather agent on *query* and log every lifecycle event.

    Args:
        query: The user question to send to the agent.
    """
    print(f"\nQuery: {query!r}")
    print("-" * 60)

    result = Runner.run_streamed(weather_agent, query)
    logger = EventLogger()

    async for event in result.stream_events():
        if isinstance(event, AgentUpdatedStreamEvent):
            logger.log("AGENT", f"Agent turn started — {event.new_agent.name}")

        elif isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                delta = event.data.delta
                if delta:
                    logger.log("TEXT", f"Text chunk: {delta[:30]!r}")
                    logger.text_chunks += 1

        elif isinstance(event, RunItemStreamEvent):
            item = event.item
            if item.type == "tool_call_item":
                tool_name = getattr(item.raw_item, "name", "unknown")
                logger.log("TOOL_CALL", f"Tool call: {tool_name}")
                logger.tool_calls += 1
            elif item.type == "tool_call_output_item":
                preview = str(item.output)[:50]
                logger.log("TOOL_RESULT", f"Tool result: {preview!r}")

    logger.log("TEXT", f"Final output: {result.final_output!r}")
    logger.summary()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run two test queries under a single trace."""
    queries = [
        "What's the weather in Tokyo and what time is it in JST?",
        "Compare weather in London and Paris.",
    ]

    with trace("streaming_lifecycle_events"):
        for index, query in enumerate(queries):
            if index > 0:
                print("\n" + "=" * 60)
            await run_with_event_logging(query)


if __name__ == "__main__":
    asyncio.run(main())
