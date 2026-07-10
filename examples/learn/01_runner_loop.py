"""Example 01: Runner core loop.

Principle (see docs/architecture.md §1):
Runner.run follows a 4-step loop - 1) Call model 2) Get final output 3) Get handoff 4) Get tool call
Source: agents/run.py:768-1498 (AgentRunner main loop)
      agents/run.py:450-1564 (AgentRunner.run overall)

Observe three things in this example:
  - Single turn, single step: Model returns message once, no tool call -> takes FinalOutput branch
  - With tool: Model might call tool, triggering two rounds "tool call -> model again -> final"
  - Use lifecycle hooks to print "every beat in the loop"

Usage: python examples/01_runner_loop.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import MODEL  # type: ignore[import-not-found]

from agents import Agent, RunHooks, Runner, function_tool


#  1) A "heartbeat" hook injected into the runner
class HeartbeatHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        print(f"   [hook] on_agent_start: {agent.name}")

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        print(f"   [hook] on_llm_start:  agent={agent.name} items={len(input_items)}")

    async def on_llm_end(self, context, agent, response):
        print(f"   [hook] on_llm_end:    output_items={len(response.output)}")

    async def on_tool_start(self, context, agent, tool):
        print(f"   [hook] on_tool_start: {tool.name}")

    async def on_tool_end(self, context, agent, tool, result):
        print(f"   [hook] on_tool_end:   {tool.name} -> {str(result)[:60]}")

    async def on_agent_end(self, context, agent, output):
        print(f"   [hook] on_agent_end:  {agent.name} final={str(output)[:60]}")


#  2) A tool to observe the loop taking the "tool call -> call model again" branch
@function_tool
def word_count(text: str) -> int:
    """Count words in a string (Chinese by char, English by word)."""
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    en = len([w for w in text.split() if any(c.isalpha() for c in w)])
    return cn + en


async def scenario(label: str, agent: Agent, prompt: str, hooks):
    print(f"\n {label} ")
    result = await Runner.run(agent, prompt, hooks=hooks)
    print(f"   Turns (new_items count): {len(result.new_items)}")
    print(f"   Final agent:           {result.last_agent.name}")
    print(f"   final_output:         {result.final_output}")


async def main():
    hooks = HeartbeatHooks()

    # Scenario A: No tool. Model returns once -> NextStepFinalOutput
    plain = Agent(
        name="PlainAgent",
        instructions="Answer in one or two sentences.",
        model=MODEL,
    )
    await scenario("A) Single turn, no tool", plain, "Hello", hooks)

    # Scenario B: With tool. Model calls word_count -> NextStepRunAgain -> returns final in next round
    tool_agent = Agent(
        name="ToolAgent",
        instructions="When the user provides text, you must first count it using word_count, then tell the user the result.",
        model=MODEL,
        tools=[word_count],
    )
    await scenario(
        "B) With tool -> at least 2 rounds",
        tool_agent,
        "Count the words in this sentence: 'OpenAI Agents SDK is great'",
        hooks,
    )


if __name__ == "__main__":
    asyncio.run(main())
