from __future__ import annotations

import asyncio
import importlib.metadata
import os

from openai import AsyncOpenAI

from agents import (
    Agent,
    Model,
    ModelProvider,
    OpenAIChatCompletionsModel,
    RunConfig,
    Runner,
    function_tool,
    set_tracing_disabled,
)

"""Use Perplexity's Agent API (OpenAI-compatible chat completions) with the Agents SDK.

Perplexity's chat completions endpoint is OpenAI-compatible, so the simplest path is to
point an `AsyncOpenAI` client at `https://api.perplexity.ai` and wrap it in a
`ModelProvider` that returns an `OpenAIChatCompletionsModel`.

Set `PERPLEXITY_API_KEY` in your environment before running:

    export PERPLEXITY_API_KEY="..."
    uv run examples/model_providers/perplexity_provider.py

Docs: https://docs.perplexity.ai/api-reference/chat-completions-post
"""

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
DEFAULT_MODEL = "sonar-pro"
INTEGRATION_SLUG = "openai-agents"


def _attribution_header() -> str:
    try:
        version = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return f"{INTEGRATION_SLUG}/{version}"


API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
if not API_KEY:
    raise ValueError("Please set PERPLEXITY_API_KEY in your environment.")


client = AsyncOpenAI(
    base_url=PERPLEXITY_BASE_URL,
    api_key=API_KEY,
    default_headers={"X-Pplx-Integration": _attribution_header()},
)
set_tracing_disabled(disabled=True)


class PerplexityProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        return OpenAIChatCompletionsModel(
            model=model_name or DEFAULT_MODEL,
            openai_client=client,
        )


PERPLEXITY_PROVIDER = PerplexityProvider()


@function_tool
def get_weather(city: str) -> str:
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful research assistant. Cite sources when useful.",
        tools=[get_weather],
    )

    result = await Runner.run(
        agent,
        "What are the latest developments in quantum computing this week?",
        run_config=RunConfig(model_provider=PERPLEXITY_PROVIDER),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
