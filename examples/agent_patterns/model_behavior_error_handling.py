"""Handling ModelBehaviorError — three patterns.

``ModelBehaviorError`` is raised when the model does something the SDK cannot
handle: calling a tool that does not exist, returning malformed JSON for a
structured output, or producing an output that fails schema validation.

This file demonstrates three practical ways to deal with it, in increasing
order of complexity.

Pattern 1 — Ignore and return a fallback value.
    Useful when the task is best-effort and a graceful default is acceptable.

Pattern 2 — Log and re-raise with context.
    Useful in production pipelines where you want structured error reporting
    without losing the original exception.

Pattern 3 — Retry with a fallback agent.
    When the primary model misbehaves, hand the same task to a more reliable
    (often larger) fallback model.

Run with:
    python -m examples.agent_patterns.model_behavior_error_handling
"""

import asyncio
from dataclasses import dataclass

from agents import Agent, ModelBehaviorError, Runner, function_tool


@function_tool
def get_stock_price(ticker: str) -> str:
    """Return a (mocked) stock price for the given ticker symbol.

    Args:
        ticker: Stock ticker symbol, e.g. AAPL.
    """
    prices = {"AAPL": "$189.30", "GOOGL": "$175.20", "MSFT": "$415.50"}
    return prices.get(ticker.upper(), f"Ticker '{ticker}' not found.")


primary_agent = Agent(
    name="Stock Assistant",
    instructions=(
        "You are a stock price assistant. Use the get_stock_price tool to answer questions. "
        "Do not call any other tools."
    ),
    tools=[get_stock_price],
)

# A more conservative fallback agent using explicit instructions to reduce errors.
fallback_agent = Agent(
    name="Stock Assistant (fallback)",
    instructions=(
        "You are a stock price assistant. You have ONE tool: get_stock_price(ticker). "
        "Call it exactly once with the ticker symbol the user mentioned. "
        "Do not call any other tool. Do not make up data."
    ),
    tools=[get_stock_price],
)


# ---------------------------------------------------------------------------
# Pattern 1 — Return a safe default on error
# ---------------------------------------------------------------------------


async def pattern_1_fallback_value(task: str) -> str:
    """Run the agent; return a safe default string if ModelBehaviorError occurs."""
    try:
        result = await Runner.run(primary_agent, task)
        return result.final_output
    except ModelBehaviorError as exc:
        print(f"[pattern-1] ModelBehaviorError: {exc.message}")
        return "Sorry, I could not retrieve that information right now."


# ---------------------------------------------------------------------------
# Pattern 2 — Log and re-raise with enriched context
# ---------------------------------------------------------------------------


@dataclass
class EnrichedModelError(Exception):
    original: ModelBehaviorError
    task: str
    agent_name: str

    def __str__(self) -> str:
        return (
            f"Agent '{self.agent_name}' failed on task: {self.task!r}\n"
            f"Reason: {self.original.message}"
        )


async def pattern_2_log_and_reraise(task: str) -> str:
    """Run the agent; re-raise ModelBehaviorError with structured context."""
    try:
        result = await Runner.run(primary_agent, task)
        return result.final_output
    except ModelBehaviorError as exc:
        raise EnrichedModelError(
            original=exc,
            task=task,
            agent_name=primary_agent.name,
        ) from exc


# ---------------------------------------------------------------------------
# Pattern 3 — Retry with a fallback agent
# ---------------------------------------------------------------------------


async def pattern_3_fallback_agent(task: str) -> str:
    """Try the primary agent; fall back to a more reliable agent on error."""
    try:
        result = await Runner.run(primary_agent, task)
        print("[pattern-3] Primary agent succeeded.")
        return result.final_output
    except ModelBehaviorError as exc:
        print(f"[pattern-3] Primary agent failed ({exc.message}), trying fallback agent...")
        result = await Runner.run(fallback_agent, task)
        print("[pattern-3] Fallback agent succeeded.")
        return result.final_output


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


async def main() -> None:
    task = "What is the current price of AAPL stock?"

    print("=" * 60)
    print("Pattern 1 — fallback value")
    print("=" * 60)
    output = await pattern_1_fallback_value(task)
    print(f"Output: {output}\n")

    print("=" * 60)
    print("Pattern 2 — log and re-raise")
    print("=" * 60)
    try:
        output = await pattern_2_log_and_reraise(task)
        print(f"Output: {output}\n")
    except EnrichedModelError as exc:
        print(f"Caught enriched error:\n{exc}\n")

    print("=" * 60)
    print("Pattern 3 — fallback agent")
    print("=" * 60)
    output = await pattern_3_fallback_agent(task)
    print(f"Output: {output}\n")


if __name__ == "__main__":
    asyncio.run(main())
