"""Example 02: RunHooks vs AgentHooks.

Principle:
- RunHooks is bound to Runner.run(hooks=...), observes the whole run (including handoffs)
- AgentHooks is bound to agent.hooks = ..., observes only this agent's events
- Event set: on_agent_start / on_agent_end / on_llm_start / on_llm_end /
         on_tool_start / on_tool_end / on_handoff
Source: agents/lifecycle.py:13-200

Observe:
  1) RunHooks receives events from all agents; AgentHooks receives only its own
  2) on_handoff triggers only once in RunHooks (cross-agent perspective)
  3) on_agent_start triggers every turn (whenever current_agent switches)

Usage: python examples/02_lifecycle_hooks.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_defs.math_agent import math_agent  # type: ignore[import-not-found]
from agent_defs.triage_agent import triage_agent  # type: ignore[import-not-found]
from agent_defs.weather_agent import weather_agent  # type: ignore[import-not-found]

from agents import AgentHooks, RunHooks, Runner


class GlobalRunLog(RunHooks):
    def __init__(self):
        self.events: list[str] = []

    def _log(self, tag: str, agent: str):
        self.events.append(f"[RUN ] {tag} {agent}")

    async def on_agent_start(self, context, agent):
        self._log("agent_start", agent.name)

    async def on_agent_end(self, context, agent, output):
        self._log("agent_end", agent.name)

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        self._log("llm_start", agent.name)

    async def on_llm_end(self, context, agent, response):
        self._log("llm_end", agent.name)

    async def on_tool_start(self, context, agent, tool):
        self._log("tool_start", f"{agent.name}/{tool.name}")

    async def on_tool_end(self, context, agent, tool, result):
        self._log("tool_end", f"{agent.name}/{tool.name}")

    async def on_handoff(self, context, from_agent, to_agent):
        self._log("HANDOFF", f"{from_agent.name} -> {to_agent.name}")


class PerAgentLog(AgentHooks):
    """Bound to a single agent, receives only events triggered by itself"""

    def __init__(self, owner: str):
        self.owner = owner
        self.events: list[str] = []

    def _log(self, tag: str):
        self.events.append(f"[AGENT {self.owner}] {tag}")

    async def on_start(self, context, agent):
        self._log("on_start")

    async def on_end(self, context, agent, output):
        self._log("on_end")

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        self._log("llm_start")

    async def on_llm_end(self, context, agent, response):
        self._log("llm_end")

    async def on_tool_start(self, context, agent, tool):
        self._log(f"tool_start/{tool.name}")

    async def on_tool_end(self, context, agent, tool, result):
        self._log(f"tool_end/{tool.name}")


async def main():
    run_log = GlobalRunLog()

    # Bind a "self-only" AgentHooks to math_agent
    math_log = PerAgentLog("MathAgent")
    math_agent.hooks = math_log
    weather_log = PerAgentLog("WeatherAgent")
    weather_agent.hooks = weather_log

    print(">>> User: What is 3 plus 5?\n")
    result = await Runner.run(triage_agent, "What is 3 plus 5?", hooks=run_log)
    print(f"<<< Agent: {result.final_output}\n")

    print("=== Events received by global RunHooks ===")
    for e in run_log.events:
        print(" ", e)
    print(f"\n=== MathAgent's own hooks received ({len(math_log.events)} items) ===")
    for e in math_log.events:
        print(" ", e)
    print(
        f"\n WeatherAgent's own hooks received ({len(weather_log.events)} items, should be 0) ==="
    )
    for e in weather_log.events:
        print(" ", e)

    print("\nObserve:")
    print(" - RunHooks contains HANDOFF: triage -> math_agent")
    print(" - MathAgent's AgentHooks has events; WeatherAgent has none (didn't switch to it)")


if __name__ == "__main__":
    asyncio.run(main())
