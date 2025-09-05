import asyncio
import logging

from openai import AsyncOpenAI

from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled

set_tracing_disabled(True)
logging.basicConfig(level=logging.DEBUG)

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


async def main():
    # Note that using a custom outputType for an agent may not work well with gpt-oss models.
    # Consider going with the default "text" outputType.
    # See also: https://github.com/openai/openai-agents-python/issues/1414
    agent = Agent(
        name="Assistant",
        instructions="You're a helpful assistant. You provide a concise answer to the user's question.",
        model=gpt_oss_model,
    )

    result = await Runner.run(agent, "Tell me about recursion in programming.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
