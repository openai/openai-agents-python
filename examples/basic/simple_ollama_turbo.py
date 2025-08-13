import asyncio

from agents.extensions.models.litellm_model import LitellmModel

from agents import (
    Agent,
    ModelSettings,
    Runner,
    enable_verbose_stdout_logging,
    set_tracing_disabled,
)

lite_llm_model = LitellmModel(
    model="ollama_chat/gpt-oss:20b",
    base_url="https://ollama.com",
)
set_tracing_disabled(disabled=True)
enable_verbose_stdout_logging()


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=lite_llm_model,
        model_settings=ModelSettings(
            extra_headers={
                "Authorization": "Bearer <Ollama API Key>"  # Create a key at https://ollama.com/settings/keys
            },
        ),
    )

    result = await Runner.run(
        agent, max_turns=3, input="Tell me about recursion in programming."
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
