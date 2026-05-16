from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from openai import AsyncOpenAI

from agents import (
    Agent,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)

DEFAULT_QIANFAN_BASE_URL = "https://qianfan.baidubce.com/v2"
DEFAULT_QIANFAN_MODEL = "ernie-5.0"


@dataclass(frozen=True)
class QianfanExampleSettings:
    base_url: str
    api_key: str
    model_name: str


def load_settings() -> QianfanExampleSettings:
    return QianfanExampleSettings(
        base_url=os.getenv("QIANFAN_BASE_URL", DEFAULT_QIANFAN_BASE_URL),
        api_key=os.getenv("QIANFAN_API_KEY", "dummy"),
        model_name=os.getenv("QIANFAN_MODEL", DEFAULT_QIANFAN_MODEL),
    )


def configure_client(settings: QianfanExampleSettings) -> None:
    client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key)
    set_default_openai_client(client=client, use_for_tracing=False)
    set_default_openai_api("chat_completions")
    set_tracing_disabled(disabled=True)


@function_tool
def get_weather(city: str) -> str:
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main(settings: QianfanExampleSettings | None = None) -> str | None:
    settings = settings or load_settings()
    if settings.api_key == "dummy":
        message = "Skipping run because no valid QIANFAN_API_KEY was provided."
        print(message)
        return message

    configure_client(settings)

    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=settings.model_name,
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Beijing?")
    print(result.final_output)
    return str(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
