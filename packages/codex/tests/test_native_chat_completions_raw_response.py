"""Detached Chat client JSON on the existing native observation stream."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents import (
    Agent,
    ChatCompletionsModel,
    LitellmModel,
    ModelRequest,
    NativeError,
    NativeProvider,
    RunDiagnosticError,
    function_tool,
)
from agents.observation import RawProviderEvent
from tests._asyncio import timeout

MARKER = "SYNTHETIC_CHAT_RESPONSE_EXTENSION_a8fd31"


def chat_reply(tool_call=False):
    message = {"role": "assistant", "content": "done"}
    if tool_call:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_fail",
                    "type": "function",
                    "function": {"name": "fail", "arguments": "{}"},
                }
            ],
        }
    return {
        "id": "chat_failure_fixture",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_call else "stop",
                "message": message,
            }
        ],
        "provider_extension": {"nested": [MARKER]},
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_chat_provider_json_native_opt_in_detachment_and_turn_isolation(enabled):
    requests = []
    responses = []

    class Completions:
        async def create(self, **body):
            requests.append(deepcopy(body))
            response = {
                "id": f"chat_completion_{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "provider_extension": {"nested": [MARKER]},
            }
            responses.append(response)
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    async with (
        timeout(20),
        NativeProvider(
            base_url="https://example.invalid/v1", model_backend=ChatCompletionsModel(client)
        ) as provider,
    ):
        async with Agent(instructions="Reply briefly.", model="fixture", provider=provider).session(
            include_raw_provider_events=enabled, observation_retention="all"
        ) as session:
            first = await session.submit("First.")
            assert (await first.get_result()).final_output == "done"
            original = deepcopy(responses[0])
            responses[0]["provider_extension"]["nested"][0] = "changed after completion"
            frames = [
                event
                async for event in first.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert [event.payload for event in frames] == ([original] if enabled else [])
            assert all(
                event.source == "chat_completions"
                and event.transport == "json"
                and event.turn_id == first.turn_id
                and event.submission_id == first.submission_id
                for event in frames
            )
            assert not [
                event async for event in first.events() if isinstance(event, RawProviderEvent)
            ]
            second = await session.submit("Second.")
            assert (await second.get_result()).final_output == "done"
            assert MARKER not in repr(requests[1]["messages"])
            later = [
                event
                async for event in second.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert [event.payload["id"] for event in later] == (
                ["chat_completion_2"] if enabled else []
            )


@pytest.mark.parametrize("object_field", [False, True])
async def test_chat_json_in_native_failure_details(object_field):
    cause = ValueError("synthetic tool failure")
    response = chat_reply(True)
    if not object_field:
        response.pop("object")

    @function_tool(error_function=None)
    def fail() -> str:
        raise cause

    class Completions:
        async def create(self, **_body):
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    async with (
        timeout(20),
        NativeProvider(
            base_url="https://example.invalid/v1", model_backend=ChatCompletionsModel(client)
        ) as provider,
    ):
        agent = Agent(instructions="Call fail.", model="fixture", provider=provider, tools=[fail])
        with pytest.raises(RunDiagnosticError) as caught:
            await agent.run("Fail after model output.", capture_error_details=True)
    assert caught.value.__cause__ is cause
    assert caught.value.details.raw_responses == (response,)
    assert MARKER not in repr(caught.value)


async def test_cancelled_chat_client_cleanup_without_late_raw_json():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    class Completions:
        async def create(self, **_body):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return chat_reply()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    async with (
        timeout(20),
        NativeProvider(
            base_url="https://example.invalid/v1", model_backend=ChatCompletionsModel(client)
        ) as provider,
    ):
        async with Agent(instructions="Reply briefly.", model="fixture", provider=provider).session(
            include_raw_provider_events=True
        ) as session:
            turn = await session.submit("Wait.")
            outcome = asyncio.create_task(turn.get_result())
            await entered.wait()
            stopping = asyncio.create_task(turn.cancel())
            try:
                await asyncio.wait_for(cancelled.wait(), timeout=5)
                assert not stopping.done()
            finally:
                release.set()
            await stopping
            await asyncio.gather(outcome, return_exceptions=True)
            assert not [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]


@pytest.mark.parametrize("capture", [False, True])
async def test_litellm_refusal_retains_original_client_json(capture):
    response = chat_reply()
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "provider_specific_fields": {"refusal": "synthetic refusal"},
    }
    original = deepcopy(response)

    class Client:
        async def acompletion(self, **_body):
            return SimpleNamespace(model_dump=lambda: deepcopy(response))

    async with (
        timeout(20),
        NativeProvider(
            base_url="https://example.invalid/v1", model_backend=LitellmModel(Client())
        ) as provider,
    ):
        async with Agent(instructions="Reply briefly.", model="fixture", provider=provider).session(
            include_raw_provider_events=capture, observation_retention="all"
        ) as session:
            turn = await session.submit("Reply.")
            with pytest.raises(NativeError) as refused:
                await turn.get_result()
            assert refused.value.response_integrity_reason == "provider_refusal"
            frames = [
                event.payload
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
    assert frames == ([original] if capture else [])
    assert response == original


async def test_litellm_refusal_adapter_keeps_client_projection_separate():
    response = chat_reply()
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "provider_specific_fields": {"refusal": "synthetic refusal"},
    }

    class Client:
        async def acompletion(self, **_body):
            return SimpleNamespace(model_dump=lambda: deepcopy(response))

    request = ModelRequest(
        {"model": "fixture", "input": []}, None, "fixture-session", "fixture-turn"
    )
    object.__setattr__(request, "_observe_raw_provider_events", True)
    result = await LitellmModel(Client()).complete(request)
    assert result.output[0]["content"] == [{"type": "refusal", "refusal": "synthetic refusal"}]
    assert result._raw_chat_completion == response


def test_chat_parameter_failure_does_not_retain_message_content_in_traceback() -> None:
    secret = "synthetic private Chat prompt"
    model = ChatCompletionsModel(SimpleNamespace())
    request = ModelRequest(
        body={
            "model": "fixture",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": secret}],
                }
            ],
            "tools": [{"type": "file_search"}],
        },
        context=None,
        session_id="test-session",
        turn_id="test-turn",
    )
    with pytest.raises(ValueError, match="ChatCompletionsModel") as rejected:
        model._parameters(request, streaming=True)
    frame = rejected.traceback[-1].frame
    assert frame.code.name == "_parameters"
    assert secret not in repr(frame.f_locals)
