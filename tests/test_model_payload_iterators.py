from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, cast

import httpx
import pytest
from openai import NOT_GIVEN
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.responses import ToolParam

from agents import (
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    generation_span,
)
from agents.models import (
    openai_chatcompletions as chat_module,
    openai_responses as responses_module,
)


class _SingleUseIterable:
    """Helper iterable that raises if iterated more than once."""

    def __init__(self, values: list[object]) -> None:
        self._values = list(values)
        self.iterations = 0

    def __iter__(self) -> Iterator[object]:
        if self.iterations:
            raise RuntimeError("Iterable should have been materialized exactly once.")
        self.iterations += 1
        yield from self._values


def _force_materialization(value: object) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _force_materialization(nested)
    elif isinstance(value, list):
        for nested in value:
            _force_materialization(nested)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        list(value)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completions_materializes_iterator_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_iter = _SingleUseIterable([{"type": "text", "text": "hi"}])
    tool_iter = _SingleUseIterable([{"type": "string"}])

    chat_converter = cast(Any, chat_module).Converter

    monkeypatch.setattr(
        chat_converter,
        "items_to_messages",
        classmethod(lambda _cls, _input: [{"role": "user", "content": message_iter}]),
    )
    monkeypatch.setattr(
        chat_converter,
        "tool_to_openai",
        classmethod(
            lambda _cls, _tool: {
                "type": "function",
                "function": {
                    "name": "dummy",
                    "parameters": {"properties": tool_iter},
                },
            }
        ),
    )

    captured_kwargs: dict[str, Any] = {}

    class DummyCompletions:
        async def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            _force_materialization(kwargs["messages"])
            if kwargs["tools"] is not NOT_GIVEN:
                _force_materialization(kwargs["tools"])
            return ChatCompletion(
                id="dummy-id",
                created=0,
                model="gpt-4",
                object="chat.completion",
                choices=[],
                usage=None,
            )

    class DummyClient:
        def __init__(self) -> None:
            self.chat = type("_Chat", (), {"completions": DummyCompletions()})()
            self.base_url = httpx.URL("http://example.test")

    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=DummyClient())  # type: ignore[arg-type]

    with generation_span(disabled=True) as span:
        await cast(Any, model)._fetch_response(
            system_instructions=None,
            input="ignored",
            model_settings=ModelSettings(),
            tools=[object()],
            output_schema=None,
            handoffs=[],
            span=span,
            tracing=ModelTracing.DISABLED,
            stream=False,
        )

    assert message_iter.iterations == 1
    assert tool_iter.iterations == 1
    assert isinstance(captured_kwargs["messages"][0]["content"], list)
    assert isinstance(captured_kwargs["tools"][0]["function"]["parameters"]["properties"], list)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_responses_materializes_iterator_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    input_iter = _SingleUseIterable([{"type": "input_text", "text": "hello"}])
    tool_iter = _SingleUseIterable([{"type": "string"}])

    responses_item_helpers = cast(Any, responses_module).ItemHelpers
    responses_converter = cast(Any, responses_module).Converter

    monkeypatch.setattr(
        responses_item_helpers,
        "input_to_new_input_list",
        classmethod(lambda _cls, _input: [{"role": "user", "content": input_iter}]),
    )

    converted_tools = responses_module.ConvertedTools(
        tools=cast(
            list[ToolParam],
            [
                {
                    "type": "function",
                    "name": "dummy",
                    "parameters": {"properties": tool_iter},
                }
            ],
        ),
        includes=[],
    )
    monkeypatch.setattr(
        responses_converter,
        "convert_tools",
        classmethod(lambda _cls, _tools, _handoffs: converted_tools),
    )

    captured_kwargs: dict[str, Any] = {}

    class DummyResponses:
        async def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            _force_materialization(kwargs["input"])
            _force_materialization(kwargs["tools"])
            return object()

    class DummyClient:
        def __init__(self) -> None:
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4.1", openai_client=DummyClient())  # type: ignore[arg-type]

    await cast(Any, model)._fetch_response(
        system_instructions=None,
        input="ignored",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert input_iter.iterations == 1
    assert tool_iter.iterations == 1
    assert isinstance(captured_kwargs["input"][0]["content"], list)
    assert isinstance(captured_kwargs["tools"][0]["parameters"]["properties"], list)
