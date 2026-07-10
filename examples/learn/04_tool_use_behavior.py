"""Example 04: tool_use_behavior - "Next step" control after tool execution.

Principle (agents/agent.py:345-365, 4 valid values):
  - "run_llm_again" (default): Feed tool result back to LLM, let LLM decide final
  - "stop_on_first_tool": First tool output used as final directly, skips LLM
  - StopAtTools(stop_at_tool_names=[...]): Stops when any tool in list is triggered
  - ToolsToFinalOutputFunction: Custom (ctx, tool_results) -> ToolsToFinalOutputResult
                                  (returns dataclass, not raw string)

Compare scenarios to see behavioral differences.

Usage: python examples/04_tool_use_behavior.py
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import MODEL  # type: ignore[import-not-found]

from agents import (
    Agent,
    RunContextWrapper,
    Runner,
    StopAtTools,
    ToolsToFinalOutputResult,
    function_tool,
)


@function_tool
def lookup_user(user_id: int) -> str:
    """Look up user information.

    Args:
        user_id: User ID
    """
    return json.dumps({"id": user_id, "name": "Alice", "role": "admin"}, ensure_ascii=False)


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send email.

    Args:
        to:      Recipient
        subject: Subject
        body:    Body
    """
    return f"Sent email to {to}: {subject}"


#  Scenario 1: Default run_llm_again 
def make_default_agent():
    return Agent(
        name="DefaultAgent",
        instructions="Call lookup_user for user info; call send_email for email. Keep answers concise.",
        model=MODEL,
        tools=[lookup_user, send_email],
    )


#  Scenario 2: stop_on_first_tool 
def make_stop_agent():
    return Agent(
        name="StopAgent",
        instructions="Call lookup_user for user info.",
        model=MODEL,
        tools=[lookup_user, send_email],
        tool_use_behavior="stop_on_first_tool",
    )


#  Scenario 3: StopAtTools - Stop only on send_email 
def make_stop_at_email_agent():
    return Agent(
        name="StopAtEmailAgent",
        instructions="Call lookup_user for user info; call send_email when user asks to send email.",
        model=MODEL,
        tools=[lookup_user, send_email],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["send_email"]),
    )


#  Scenario 4: Custom function - Must return ToolsToFinalOutputResult 
def custom_final(ctx: RunContextWrapper, tool_results: list[Any]) -> ToolsToFinalOutputResult:
    """Return None to continue; return ToolsToFinalOutputResult to end."""
    for r in tool_results:
        if r.tool.name == "lookup_user":
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=f"[Custom wrapper] {r.output}",
            )
    return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


def make_custom_agent():
    return Agent(
        name="CustomAgent",
        instructions="Call lookup_user for user info.",
        model=MODEL,
        tools=[lookup_user],
        tool_use_behavior=custom_final,
    )


async def run(label: str, agent: Agent, prompt: str):
    print(f"\n {label}")
    print(f"  tool_use_behavior = {agent.tool_use_behavior}")
    result = await Runner.run(agent, prompt)
    print(f"  final_output:     {result.final_output}")
    print(f"  new_items count:  {len(result.new_items)}")
    # See type of each item

    types = [type(it).__name__ for it in result.new_items]
    print(f"  item type sequence: {types}")


async def main():
    await run("A) Default run_llm_again", make_default_agent(), "Look up info for user 42")
    await run("B) stop_on_first_tool", make_stop_agent(), "Look up info for user 42")
    await run("C) StopAtTools(send_email)", make_stop_at_email_agent(), "Look up info for user 42")
    await run("D) Custom function custom_final", make_custom_agent(), "Look up info for user 42")


if __name__ == "__main__":
    asyncio.run(main())
