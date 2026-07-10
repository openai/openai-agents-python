"""Example 06: agent.as_tool() - Manager pattern.

Principle (agents/agent.py:508-936):
  agent.as_tool(tool_name, tool_description) wraps an agent into a FunctionTool,
  allowing another agent to "call" it (unlike handoff: control returns to main agent after call).
  - control flow: main agent stays; expert agent runs sub-Runner.run and feeds final_output back
  - parameters: Pydantic BaseModel, determines what parameters main agent can pass
  - custom_output_extractor: extracts parts from sub-run result to return as tool
  - needs_approval: True -> uses HITL flow (see example 10)
  - on_stream: True -> forwards streaming events from sub-run

Comparison:
  - handoff: transfers control, sub-agent takes over the whole turn
  - as_tool: main agent collects all expert results before continuing

Usage: python examples/06_agent_as_tool.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import MODEL  # type: ignore[import-not-found]
from pydantic import BaseModel, Field
from tools.calculator import CALCULATOR_TOOLS  # type: ignore[import-not-found]

from agents import Agent, Runner

# 1) An "expert agent", only does addition
adder_agent = Agent(
    name="AdderAgent",
    instructions="Add two numbers. Give result directly, no nonsense.",
    model=MODEL,
)


# 2) Wrap as FunctionTool via as_tool, and define structured input
class AddArgs(BaseModel):
    a: int = Field(description="First number")
    b: int = Field(description="Second number")


adder_tool = adder_agent.as_tool(
    tool_name="add_numbers",
    tool_description="Add two numbers and return result",
    parameters=AddArgs,
)


# 3) An "expert agent" for long text summarization
summarizer_agent = Agent(
    name="SummarizerAgent",
    instructions="Summarize the user's text in one sentence.",
    model=MODEL,
)

summarize_tool = summarizer_agent.as_tool(
    tool_name="summarize",
    tool_description="Compress a long text into one sentence",
)


# 4) A "manager agent" using the two experts above
manager = Agent(
    name="ManagerAgent",
    instructions=(
        "You are manager agent.\n"
        "User gives task:\n"
        "  - Arithmetic issue -> call add_numbers\n"
        "  - Summarization issue -> call summarize\n"
        "Tell user the result after calling."
    ),
    model=MODEL,
    tools=[adder_tool, summarize_tool, *CALCULATOR_TOOLS],
)


async def main():
    # See the tool list received by manager
    print("=== Tool list received by ManagerAgent ===")
    for t in manager.tools:
        name = getattr(t, "name", "unknown")
        desc = getattr(t, "description", "")
        print(f"  - {name}: {desc[:60]}")
    print()

    queries = [
        "Calculate 7 plus 9 using add_numbers",
        "Summarize using summarize: 'OpenAI Agents SDK is a Python framework for building multi-agent systems, core concepts include Agent, Runner, Tools, Handoffs, Guardrails, Sessions and Tracing.'",
        "Add 1 and 2 first, then add 3 and 4",
    ]
    for q in queries:
        print(f">>> User: {q}")
        result = await Runner.run(manager, q)
        print(f"<<< Agent: {result.final_output}")
        print(
            f"   new_items count: {len(result.new_items)}  last_agent: {result.last_agent.name}\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
