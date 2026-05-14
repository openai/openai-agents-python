"""Example: Synap memory tools.

Demonstrates how to give an OpenAI Agent persistent, cross-session memory using
Synap (https://maximem.ai) — a managed memory layer for AI agents.

`synap-openai-agents` provides two tool factories:
  - `create_search_tool` — semantic search over the user's stored memories
  - `create_store_tool`  — persist explicit facts to the user's memory

Both return async callables you can wrap with `@function_tool` or pass to
`FunctionTool` directly.

Install:
    pip install synap-openai-agents maximem-synap openai-agents

Set `SYNAP_API_KEY` in your environment. Get a free key at
https://synap.maximem.ai.

Open source: https://github.com/maximem-ai/maximem_synap_sdk
"""

import asyncio
import os

from agents import Agent, FunctionTool, Runner
from maximem_synap import MaximemSynapSDK
from synap_openai_agents import create_search_tool, create_store_tool


async def main() -> None:
    sdk = MaximemSynapSDK(api_key=os.environ["SYNAP_API_KEY"])
    await sdk.initialize()

    user_id = "demo-user-001"

    search_fn = create_search_tool(sdk, user_id=user_id, customer_id="acme_corp")
    store_fn = create_store_tool(sdk, user_id=user_id, customer_id="acme_corp")

    agent = Agent(
        name="memory-assistant",
        instructions=(
            "You are a helpful assistant with long-term memory. "
            "Call search_memory to recall what you know about the user. "
            "Call store_memory to save important new facts."
        ),
        tools=[
            FunctionTool(
                search_fn,
                name="search_memory",
                description="Search the user's long-term memory.",
            ),
            FunctionTool(
                store_fn,
                name="store_memory",
                description="Store an explicit fact in the user's long-term memory.",
            ),
        ],
    )

    print("=== Turn 1: teach the agent something ===")
    result = await Runner.run(
        agent,
        "I'm a software engineer who prefers concise answers and is allergic to peanuts.",
    )
    print(result.final_output)

    print("\n=== Turn 2: agent recalls from Synap ===")
    result = await Runner.run(agent, "What do you know about my dietary restrictions?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
