from __future__ import annotations

import base64
import io
import wave
from typing import Any, cast

import pytest

from agents import Agent, FunctionTool, NativeProvider, UserInput
from agents.agent import ModelSettings
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_session_input import IMAGE

MARKER = {"mode": "explicit"}
SETTINGS: ModelSettings = {"prompt_cache_mode": "explicit", "prompt_cache_ttl": "30m"}


def audio() -> str:
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(1)
        encoded.setsampwidth(2)
        encoded.setframerate(8000)
        encoded.writeframes(b"\0\0" * 8)
    return "data:audio/wav;base64," + base64.b64encode(output.getvalue()).decode()


def user_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["input"] if item.get("role") == "user"][0]["content"]


@pytest.mark.parametrize("kind", ["text", "image", "audio", "file"])
async def test_native_cache_breakpoint_preserves_current_content(
    responses: ResponsesFixture, kind: str
) -> None:
    part = {
        "text": {"type": "input_text", "text": "<user_instructions>user data</user_instructions>"},
        "image": {"type": "input_image", "image_url": IMAGE},
        "audio": {"type": "input_audio", "audio_url": audio()},
        "file": {"type": "input_file", "file_id": "file_synthetic_cache_document"},
    }[kind]
    responses.respond(assistant_output("plain"))
    responses.respond(assistant_output("cached"))
    settings: ModelSettings = {**SETTINGS, "input_modalities": ["text", "image", "audio"]}
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cache", supports_websockets=False
    ) as provider:
        agent = Agent(
            model="gpt-5.5", instructions="Answer.", provider=provider, model_settings=settings
        )
        assert (
            await agent.run(cast(UserInput, {"role": "user", "content": [part]}))
        ).final_output == "plain"
        marked = {**part, "prompt_cache_breakpoint": MARKER}
        assert (
            await agent.run(cast(UserInput, {"role": "user", "content": [marked]}))
        ).final_output == "cached"
    assert len(responses.requests) == 2
    plain = user_parts(responses.requests[0])
    cached = [dict(part) for part in user_parts(responses.requests[1])]
    assert cached[-1].pop("prompt_cache_breakpoint") == MARKER
    assert cached == plain
    assert responses.requests[1]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}


@pytest.mark.parametrize(
    "marker", [None, {"mode": "bad"}, {"mode": True}, {"mode": "explicit", "extra": True}]
)
async def test_native_invalid_cache_breakpoint_precedes_provider_io(
    responses: ResponsesFixture, marker: Any
) -> None:
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cache", supports_websockets=False
    ) as provider:
        agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
        with pytest.raises((ValueError, TypeError), match="cache.*breakpoint"):
            await agent.run(
                cast(
                    UserInput,
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "prefix",
                                "prompt_cache_breakpoint": marker,
                            }
                        ],
                    },
                )
            )
    assert not responses.requests


async def test_native_cache_breakpoint_survives_portable_manual_checkpoint(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(function_call("lookup", {"key": "a"}, "cache-call"))
    responses.respond(assistant_output("restored"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-cache", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            model="gpt-5.5",
            instructions="Use lookup.",
            provider=provider,
            tools=[lookup],
            model_settings=SETTINGS,
        )
        async with agent.session() as source:
            await source.submit(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "prefix",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                }
            )
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            current = await next_request(restored.interactions(), lookup)
            await current.respond("accepted")
            turn = restored.restored_turn
            assert turn is not None
            assert (await turn.get_result()).final_output == "restored"
    assert len(responses.requests) == 2
    assert user_parts(responses.requests[0]) == user_parts(responses.requests[1])
    assert user_parts(responses.requests[1])[-1]["prompt_cache_breakpoint"] == MARKER


async def test_native_initial_history_cache_breakpoint_remains_bound(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("accepted"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cache", supports_websockets=False
    ) as provider:
        result = await Agent(
            model="gpt-5.5", instructions="Answer.", provider=provider, model_settings=SETTINGS
        ).run(
            "continue",
            initial_messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "history prefix",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                }
            ],
        )
    assert result.final_output == "accepted"
    messages = [item for item in responses.requests[0]["input"] if item.get("role") == "user"]
    prefix = next(
        part
        for item in messages
        for part in item["content"]
        if part.get("text") == "history prefix"
    )
    current = next(
        part for item in messages for part in item["content"] if part.get("text") == "continue"
    )
    assert prefix["prompt_cache_breakpoint"] == MARKER
    assert "prompt_cache_breakpoint" not in current


async def test_native_borrowed_chat_preserves_cache_prefix() -> None:
    from types import SimpleNamespace

    from agents.models import ChatCompletionsModel

    calls: list[dict[str, Any]] = []

    async def create(**request: Any) -> dict[str, Any]:
        calls.append(request)
        return {
            "id": "chat_synthetic_cache",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-5.5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "accepted"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = ChatCompletionsModel(client, model="gpt-5.5")
    async with NativeProvider(
        api_key="synthetic", base_url="http://127.0.0.1:1/v1", model_backend=model
    ) as provider:
        result = await Agent(
            model="gpt-5.5", provider=provider, instructions="Answer.", model_settings=SETTINGS
        ).run(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "prefix",
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            }
        )
    assert result.final_output == "accepted"
    assert len(calls) == 1
    assert calls[0]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    user = next(message for message in calls[0]["messages"] if message["role"] == "user")
    assert user["content"] == [
        {"type": "text", "text": "prefix", "prompt_cache_breakpoint": MARKER}
    ]
