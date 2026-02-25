"""
Example: agents-as-tools with a custom model provider (issue #663)

This example demonstrates that a custom ModelProvider passed via RunConfig is
correctly inherited by nested agent-tool calls.  Previously, the model provider
was silently ignored inside Agent.as_tool(), causing the nested agents to fall
back to the default OpenAI provider and raise an error when no OPENAI_API_KEY
was set.

To run with a custom provider (e.g. OpenRouter, Ollama, any OpenAI-compatible
endpoint), export the following environment variables before running:

    export EXAMPLE_BASE_URL="https://openrouter.ai/api/v1"
    export EXAMPLE_API_KEY="<your-key>"
    export EXAMPLE_MODEL_NAME="meta-llama/llama-3.3-70b-instruct"

Then:
    python -m examples.agent_patterns.agents_as_tools_custom_provider

If the env vars are not set the script exits early with a clear message.
"""

from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI

from agents import (
    Agent,
    ItemHelpers,
    MessageOutputItem,
    Model,
    ModelProvider,
    OpenAIChatCompletionsModel,
    RunConfig,
    Runner,
    set_tracing_disabled,
)
from examples.auto_mode import input_with_fallback

BASE_URL = os.getenv("EXAMPLE_BASE_URL") or ""
API_KEY = os.getenv("EXAMPLE_API_KEY") or ""
MODEL_NAME = os.getenv("EXAMPLE_MODEL_NAME") or ""


class CustomModelProvider(ModelProvider):
    """A model provider that routes every call to a custom OpenAI-compatible endpoint."""

    def __init__(self, client: AsyncOpenAI, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def get_model(self, model_name: str | None) -> Model:
        return OpenAIChatCompletionsModel(
            model=model_name or self._model_name,
            openai_client=self._client,
        )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

spanish_agent = Agent(
    name="spanish_agent",
    instructions="You translate the user's message to Spanish",
    handoff_description="An english to spanish translator",
)

french_agent = Agent(
    name="french_agent",
    instructions="You translate the user's message to French",
    handoff_description="An english to french translator",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate. "
        "If asked for multiple translations, you call the relevant tools in order. "
        "You never translate on your own, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

synthesizer_agent = Agent(
    name="synthesizer_agent",
    instructions=(
        "You inspect translations, correct them if needed, and produce a final "
        "concatenated response."
    ),
)


async def main() -> None:
    if not BASE_URL or not API_KEY or not MODEL_NAME:
        print(
            "Skipping example: set EXAMPLE_BASE_URL, EXAMPLE_API_KEY, and "
            "EXAMPLE_MODEL_NAME to run with a custom provider."
        )
        return

    set_tracing_disabled(disabled=True)

    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    provider = CustomModelProvider(client, MODEL_NAME)
    run_config = RunConfig(model_provider=provider)

    msg = input_with_fallback(
        "Hi! What would you like translated? ",
        "Translate 'Hello, world!' to Spanish and French.",
    )

    # The run_config is passed to the top-level Runner.run().
    # Thanks to the fix for issue #663, it is automatically inherited by every
    # nested agent-tool call — no need to set it on each as_tool() separately.
    orchestrator_result = await Runner.run(
        orchestrator_agent,
        msg,
        run_config=run_config,
    )

    for item in orchestrator_result.new_items:
        if isinstance(item, MessageOutputItem):
            text = ItemHelpers.text_message_output(item)
            if text:
                print(f"  - Translation step: {text}")

    synthesizer_result = await Runner.run(
        synthesizer_agent,
        orchestrator_result.to_input_list(),
        run_config=run_config,
    )

    print(f"\n\nFinal response:\n{synthesizer_result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
