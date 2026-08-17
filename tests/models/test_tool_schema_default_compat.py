from __future__ import annotations

from typing import Any, cast

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from pydantic import BaseModel

from agents import Agent, OpenAIProvider, RunConfig, Runner, function_tool
from agents.models.openai_chatcompletions import _strip_json_schema_defaults


class _LookupRequest(BaseModel):
    limit: int = 10


@function_tool
def lookup_with_default(request: _LookupRequest) -> str:
    return str(request.limit)


class _RecordingCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.kwargs = kwargs
        return ChatCompletion(
            id="resp-id",
            created=0,
            model="fake",
            object="chat.completion",
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content="ok"),
                )
            ],
        )


class _RecordingClient:
    def __init__(self, completions: _RecordingCompletions) -> None:
        self.chat = type("_Chat", (), {"completions": completions})()
        self.base_url = httpx2.URL("https://example.openai.azure.com/openai/v1/")


def _contains_default(value: Any) -> bool:
    if isinstance(value, dict):
        return "default" in value or any(_contains_default(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_default(child) for child in value)
    return False


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strip_tool_schema_defaults", "expected_request_default"),
    [(False, True), (True, False)],
)
async def test_openai_provider_can_strip_strict_tool_schema_defaults(
    strip_tool_schema_defaults: bool,
    expected_request_default: bool,
) -> None:
    completions = _RecordingCompletions()
    client = cast(AsyncOpenAI, _RecordingClient(completions))
    provider = OpenAIProvider(
        openai_client=client,
        use_responses=False,
        strip_tool_schema_defaults=strip_tool_schema_defaults,
    )
    agent = Agent(
        name="test",
        model=provider.get_model("gpt-4o"),
        tools=[lookup_with_default],
    )

    assert _contains_default(lookup_with_default.params_json_schema)

    await Runner.run(
        agent,
        "Do not call the tool. Reply with ok.",
        run_config=RunConfig(tracing_disabled=True),
    )

    sent_tools = cast(list[dict[str, Any]], completions.kwargs["tools"])
    sent_schema = sent_tools[0]["function"]["parameters"]
    assert _contains_default(sent_schema) is expected_request_default
    # Compatibility normalization must not mutate the FunctionTool retained by the caller.
    assert _contains_default(lookup_with_default.params_json_schema)


def test_strip_json_schema_defaults_does_not_rewrite_annotation_payloads() -> None:
    schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 10,
                "examples": [{"default": "annotation data"}],
            }
        },
    }

    stripped = _strip_json_schema_defaults(schema)

    assert "default" not in stripped["properties"]["limit"]
    assert stripped["properties"]["limit"]["examples"] == [{"default": "annotation data"}]
    assert schema["properties"]["limit"]["default"] == 10
