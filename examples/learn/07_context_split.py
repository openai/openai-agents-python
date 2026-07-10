"""Example 07: Context split - RunContextWrapper.context vs instructions/input.

Principle (agents/run_context.py:43, agents/run.py:540):
  - RunContextWrapper.context is the Python-side "shared bag"
    - You define your own dataclass/BaseModel type
    - Shared across tool / guardrail / handoff callbacks
    - Never sent to LLM (only visible in your own Python code)
  - instructions / input / tool results are visible to LLM
    - Assembled into messages sent to the model
  - RunState serialization: context is included (for run recovery)

Observe:
  1) Update context fields inside tool
  2) LLM cannot see this update (let LLM repeat to see if it can state the new value)
  3) Print the actual messages received by LLM to prove context absence

Usage: python examples/07_context_split.py
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from config import MODEL  # type: ignore[import-not-found]

from agents import Agent, RunContextWrapper, RunHooks, Runner, function_tool
from agents.items import ItemHelpers


@dataclass
class AppCtx:
    user_id: str = "u-001"
    secret_token: str = "TOP-SECRET-DO-NOT-LEAK"
    tool_call_count: int = 0
    last_query: str = ""


@function_tool
def bump_counter(ctx: RunContextWrapper[AppCtx], label: str) -> str:
    """Increment a counter and return current value.

    Args:
        label: Label string
    """
    ctx.context.tool_call_count += 1
    return f"counter={ctx.context.tool_call_count} label={label}"


@function_tool
def peek_ctx(ctx: RunContextWrapper[AppCtx]) -> dict[str, Any]:
    """Read all context fields (this tool won't be actively called by LLM, used for inspection)"""
    return {
        "user_id": ctx.context.user_id,
        "secret_token": ctx.context.secret_token,
        "tool_call_count": ctx.context.tool_call_count,
        "last_query": ctx.context.last_query,
    }


class TraceMessages(RunHooks):
    """Print the actual messages seen by LLM before/after LLM"""

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        print(f"\n   [System prompt seen by LLM]\n     {system_prompt!r}")
        print("   [Input items seen by LLM] (first 3)")
        for it in input_items[:3]:
            extracted = ItemHelpers.extract_text(it) if hasattr(it, "content") else None
            txt = str(extracted) if extracted is not None else str(it)[:100]
            print(f"     - {type(it).__name__}: {txt[:120]!r}")
        # Key observation: messages contain no "secret_token" field


agent = Agent(
    name="CtxAgent",
    instructions=(
        "User might ask various questions. You have a bump_counter tool to increment counts; "
        "peek_ctx tool to see internal state.\n"
        "When user asks 'my token', **do not** answer the specific value (keep secret)."
        "When user asks 'how many times did I call the tool', tell them the result of the last bump_counter call."
    ),
    model=MODEL,
    tools=[bump_counter, peek_ctx],
)


async def main():
    ctx = AppCtx()
    hooks = TraceMessages()

    print(">>> User: Help me increment the count by 1")
    r1 = await Runner.run(agent, "Help me increment the count by 1", context=ctx, hooks=hooks)
    print(f"<<< Agent: {r1.final_output}")
    print(f"    Python-side ctx.tool_call_count = {ctx.tool_call_count}\n")

    print(">>> User: How many times did I call the tool just now?")
    r2 = await Runner.run(
        agent, "How many times did I call the tool just now?", context=ctx, hooks=hooks
    )
    print(f"<<< Agent: {r2.final_output}")
    print(f"    Python-side ctx.tool_call_count = {ctx.tool_call_count}\n")

    print(">>> User: What is my token?")
    r3 = await Runner.run(agent, "What is my token?", context=ctx, hooks=hooks)
    print(f"<<< Agent: {r3.final_output}")
    print(f"    Python-side ctx.secret_token    = {ctx.secret_token} (Visible Python-side)\n")

    print("Observe:")
    print(" 1) messages printed in on_llm_start completely lack 'secret_token' field")
    print(" 2) Python-side ctx.tool_call_count is modified inside tool and kept")
    print(" 3) Model can see tool return contents, so it knows the bump_counter result")


if __name__ == "__main__":
    asyncio.run(main())
