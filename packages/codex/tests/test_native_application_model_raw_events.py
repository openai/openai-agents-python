from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents import Agent, ModelRequest, ModelResponse, NativeProvider
from agents.observation import RawProviderEvent
from tests._asyncio import timeout
from tests.native_fixtures import text_response

EXTRA = {
    "type": "response.sdk_fixture.application_vendor",
    "extension": {"parts": [False, None, 0], "id": "synthetic-opaque"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
async def test_application_stream_preserves_opt_in_raw_event_data(capture: bool) -> None:
    added, delta, done = text_response("msg_app_raw", "Visible.")
    delta["vendor"] = {"opaque": ["nested", 0]}

    frames = [
        {"type": "response.created", "response": {"id": "resp_app_raw"}},
        EXTRA,
        added,
        delta,
        done,
        {"type": "response.completed", "response": {"id": "resp_app_raw"}},
    ]

    class Producer:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            for frame in frames:
                yield frame

    async with timeout(20), NativeProvider(model_backend=Producer()) as provider:
        agent = Agent(instructions="Respond.", model="fixture-native", provider=provider)
        async with agent.session(
            include_raw_provider_events=capture,
            observation_retention="all",
        ) as session:
            turn = await session.submit("Begin.")
            assert (await turn.get_result()).final_output == "Visible."
            raw = [
                event.payload
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
    assert raw == (frames if capture else [])
