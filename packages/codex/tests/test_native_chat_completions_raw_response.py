"""Detached Chat client JSON on the existing native observation stream."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents import Agent, ChatCompletionsModel, NativeProvider
from agents.observation import RawProviderEvent
from tests._asyncio import timeout

MARKER = "SYNTHETIC_CHAT_RESPONSE_EXTENSION_a8fd31"


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
