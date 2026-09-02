from __future__ import annotations

import asyncio
import os

from agents import Agent, Runner, trace

"""Give an agent 60,000+ tools through the Cracked tool router (https://cracked.ai).

Cracked exposes scrapers, search, social media, maps, finance, weather and AI-model tools
behind one API key with per-call billing. The `cracked-ai-openai-agents` package wraps the
API as four function tools: `cracked_discover` (find a tool in natural language),
`cracked_inspect` (read its JSON Schema), `cracked_run` (run one provider endpoint) and
`cracked_run_capability` (smart run by capability id, e.g. "web-search", with fallback).

`cracked_run` and `cracked_run_capability` take `input_json` (a JSON object string) because
strict function schemas do not allow free-form objects.

Run it like this:

    pip install cracked-ai-openai-agents
    export CRACKED_API_KEY=ck_live_...   # https://cracked.ai/app/keys
    uv run examples/tools/cracked_tool_router.py

The run below fetches a weather forecast through Cracked and costs well under $0.01.
"""


async def main() -> None:
    if not os.environ.get("CRACKED_API_KEY"):
        print(
            "Skipping run because CRACKED_API_KEY is not set (get a key at https://cracked.ai/app/keys)."
        )
        return

    try:
        from cracked_ai_openai_agents import cracked_toolkit
    except ImportError:
        print(
            "Skipping run because cracked-ai-openai-agents is not installed (pip install cracked-ai-openai-agents)."
        )
        return

    agent = Agent(
        name="Researcher",
        instructions=(
            "Answer with live data. Find the right tool with cracked_discover, read its schema with "
            "cracked_inspect, then cracked_run it with the exact fields from the schema. When the task "
            "matches a capability id such as weather-forecast or web-search, cracked_run_capability is "
            "faster. Quote output fields; never invent data."
        ),
        tools=cracked_toolkit(),
    )

    with trace("Cracked tool router example"):
        result = await Runner.run(agent, "What is the weather in Austin, TX right now?")
        print(result.final_output)
        # It is currently 31°C and clear in Austin, TX, with winds of 8 km/h ...


if __name__ == "__main__":
    asyncio.run(main())
