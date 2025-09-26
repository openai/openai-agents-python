from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses import ResponseCompletedEvent

from agents import ModelSettings, ModelTracing, __version__
from agents.models.openai_responses import _HEADERS_OVERRIDE as RESP_HEADERS, OpenAIResponsesModel
from tests.fake_model import get_response_obj


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("override_ua", [None, "test_user_agent"])
async def test_user_agent_header_responses(override_ua: str | None):
    called_kwargs: dict[str, Any] = {}
    expected_ua = override_ua or f"Agents/Python {__version__}"

    class DummyStream:
        def __aiter__(self):
            async def gen():
                yield ResponseCompletedEvent(
                    type="response.completed",
                    response=get_response_obj([]),
                    sequence_number=0,
                )

            return gen()

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return DummyStream()

    class DummyResponsesClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyResponsesClient())  # type: ignore

    if override_ua is not None:
        token = RESP_HEADERS.set({"User-Agent": override_ua})
    else:
        token = None

    try:
        stream = model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
        async for _ in stream:
            pass
    finally:
        if token is not None:
            RESP_HEADERS.reset(token)

    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["User-Agent"] == expected_ua
