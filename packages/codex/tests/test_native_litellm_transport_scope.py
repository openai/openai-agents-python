"""Keep current LiteLLM request headers scoped to the issued native Agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import Agent, NativeProvider
from agents.models import LitellmModel


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_litellm_native_handoff_changes_current_header_without_leaking(stream):
    observed = []

    async def complete(**options):
        observed.append(options.get("extra_headers"))
        identity = f"chat-{len(observed)}"
        if len(observed) == 1:
            tool_call = {
                "id": "current-profile-transfer",
                "type": "function",
                "function": {"name": "transfer_to_destination", "arguments": "{}"},
            }
            finish = "tool_calls"
            message = {"role": "assistant", "tool_calls": [tool_call]}
        else:
            finish = "stop"
            message = {"role": "assistant", "content": "Done."}
        if not stream:
            return SimpleNamespace(
                model_dump=lambda: {
                    "id": identity,
                    "choices": [{"message": message, "finish_reason": finish}],
                }
            )

        async def chunks():
            delta = dict(message)
            if finish == "tool_calls":
                delta["tool_calls"] = [{"index": 0, **tool_call}]
            yield {
                "id": identity,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        return chunks()

    backend = LitellmModel(SimpleNamespace(acompletion=complete), "test-model", stream=stream)
    async with NativeProvider(model_backend=backend) as provider:
        destination = Agent(
            name="Destination",
            instructions="Finish.",
            model_settings={"extra_headers": {"x-test-profile": "fixture-destination"}},
        )
        source = Agent(
            name="Source",
            instructions="Transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            model_settings={
                "extra_headers": {
                    "X-Test-Profile": "fixture-source",
                    "X-Test-Route": "fixture-source-only",
                }
            },
        )
        async with source.session() as session:
            result = await (await session.submit("Transfer.")).get_result()
            assert result.final_output == "Done."
            assert await session.get_active_agent() is destination

    assert [{key.lower(): value for key, value in (row or {}).items()} for row in observed] == [
        {"x-test-profile": "fixture-source", "x-test-route": "fixture-source-only"},
        {"x-test-profile": "fixture-destination"},
    ]
