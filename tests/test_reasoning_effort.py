from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage
from openai.types.shared import Reasoning

from agents import ModelSettings, ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel


class FakeClient:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None
        self.base_url = "https://example.com"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: object) -> ChatCompletion:
        self.kwargs = kwargs
        msg = ChatCompletionMessage(role="assistant", content="hi")
        choice = Choice(index=0, finish_reason="stop", message=msg)
        usage = CompletionUsage(completion_tokens=0, prompt_tokens=0, total_tokens=0)
        return ChatCompletion(
            id="resp",
            created=0,
            model="gpt-4",
            object="chat.completion",
            choices=[choice],
            usage=usage,
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_reasoning_effort_minimal() -> None:
    client = FakeClient()
    model = OpenAIChatCompletionsModel("gpt-4", cast(AsyncOpenAI, client))
    settings = ModelSettings(reasoning=Reasoning(effort="minimal"))
    await model.get_response(
        system_instructions=None,
        input="",
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    assert client.kwargs is not None
    assert client.kwargs.get("reasoning_effort") == "minimal"
