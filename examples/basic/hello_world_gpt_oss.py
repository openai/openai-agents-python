import argparse
import asyncio

from openai import AsyncOpenAI

from agents import Agent, OpenAIChatCompletionsModel, RunConfig, Runner, set_tracing_disabled

set_tracing_disabled(True)

# import logging
# logging.basicConfig(level=logging.DEBUG)

# Passing this model object has been bypassing the default provider, including CodexProvider.
# This is an example of how to use gpt-oss with Ollama.
# Refer to https://cookbook.openai.com/articles/gpt-oss/run-locally-ollama for more details.
# If you prefer using LM Studio, refer to https://cookbook.openai.com/articles/gpt-oss/run-locally-lmstudio
gpt_oss_model = OpenAIChatCompletionsModel(
    model="gpt-oss:20b",
    openai_client=AsyncOpenAI(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
)


# After `ollama pull gpt-oss:20b`, run normally for Chat Completions.
# Add --codex to run the same local model through stock Codex and Ollama's Responses API.
async def main(use_codex: bool = False):
    config = None
    if use_codex:
        from agents.models.codex import CodexProvider

        config = RunConfig(
            model_provider=CodexProvider(
                config_overrides=(
                    'model_provider="local_ollama"',
                    'model_providers.local_ollama.name="Local Ollama"',
                    'model_providers.local_ollama.base_url="http://localhost:11434/v1"',
                    'model_providers.local_ollama.wire_api="responses"',
                    "model_providers.local_ollama.supports_websockets=false",
                ),
            ),
        )
    # Note that using a custom outputType for an agent may not work well with gpt-oss models.
    # Consider going with the default "text" outputType.
    # See also: https://github.com/openai/openai-agents-python/issues/1414
    agent = Agent(
        name="Assistant",
        instructions="You're a helpful assistant. You provide a concise answer to the user's question.",
        model="gpt-oss:20b" if use_codex else gpt_oss_model,
    )

    result = await Runner.run(agent, "Tell me about recursion in programming.", run_config=config)
    print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run local gpt-oss with or without Codex.")
    parser.add_argument("--codex", action="store_true")
    asyncio.run(main(parser.parse_args().codex))
