from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai import APIConnectionError, BadRequestError, NotFoundError
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary
from typing_extensions import TypedDict

import agents._debug as _debug
from agents import (
    Agent,
    GuardrailFunctionOutput,
    Handoff,
    HandoffInputData,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRetrySettings,
    ModelSettings,
    OpenAIResponsesWSModel,
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    SQLiteSession,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    UserError,
    function_tool,
    handoff,
    retry_policies,
)
from agents.items import (
    ModelResponse,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
    TResponseStreamEvent,
)
from agents.memory.openai_conversations_session import OpenAIConversationsSession
from agents.models.interface import Model, ModelTracing
from agents.run import RunConfig
from agents.run_internal import run_loop
from agents.run_internal.run_loop import QueueCompleteSentinel
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, StreamEvent
from agents.tool import FunctionTool, Tool
from agents.tool_guardrails import tool_input_guardrail, tool_output_guardrail
from agents.usage import Usage, _attach_raw_usage_snapshot

from .fake_model import FakeModel, get_response_obj
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)
from .utils.hitl import (
    consume_stream,
    make_model_and_agent,
    queue_function_call_and_text,
    resume_streamed_after_first_approval,
)
from .utils.simple_session import CountingSession, SimpleListSession


def _conversation_locked_error() -> BadRequestError:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "conversation_locked", "message": "locked"}},
    )
    error = BadRequestError(
        "locked",
        response=response,
        body={"error": {"code": "conversation_locked"}},
    )
    error.code = "conversation_locked"
    return error


def _find_reasoning_input_item(
    items: str | list[TResponseInputItem] | Any,
) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            return cast(dict[str, Any], item)
    return None


def _ws_terminal_response_frame(event_type: str, response_id: str, sequence_number: int) -> str:
    response = get_response_obj([get_text_message("partial final")], response_id=response_id)
    return json.dumps(
        {
            "type": event_type,
            "response": response.model_dump(),
            "sequence_number": sequence_number,
        }
    )


@pytest.mark.asyncio
async def test_simple_first_run():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )
    model.set_next_output([get_text_message("first")])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "first"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert result.raw_responses[0].output == [get_text_message("first")]
    assert result.last_agent == agent

    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("second")])

    result = Runner.run_streamed(
        agent, input=[get_text_input_item("message"), get_text_input_item("another_message")]
    )
    async for _ in result.stream_events():
        pass

    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "second"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert len(result.to_input_list()) == 3, "should have original input and generated item"


@pytest.mark.asyncio
async def test_empty_list_input_reaches_model():
    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("first")])

    result = Runner.run_streamed(agent, input=[])
    async for _ in result.stream_events():
        pass

    assert result.final_output == "first"
    assert model.last_turn_args["input"] == []


@pytest.mark.asyncio
async def test_streamed_tool_not_found_behavior_returns_error_to_model() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("missing_tool", "{}", call_id="call_missing")],
            [get_text_message("recovered")],
        ]
    )

    result = Runner.run_streamed(
        agent,
        input="start",
        run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == "recovered"
    second_turn_input = model.last_turn_args["input"]
    assert isinstance(second_turn_input, list)
    assert {
        item.get("call_id"): item.get("output")
        for item in second_turn_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    } == {"call_missing": "Tool 'missing_tool' not found."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_event_type", "terminal_event_cls"),
    [
        ("response.incomplete", ResponseIncompleteEvent),
        ("response.failed", ResponseFailedEvent),
    ],
)
async def test_streamed_run_rejects_failed_terminal_response_payload_events(
    terminal_event_type: str, terminal_event_cls: type[Any]
) -> None:
    class TerminalPayloadFakeModel(FakeModel):
        async def stream_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            self.last_turn_args = {
                "system_instructions": system_instructions,
                "input": input,
                "model_settings": model_settings,
                "tools": tools,
                "output_schema": output_schema,
                "previous_response_id": previous_response_id,
                "conversation_id": conversation_id,
            }
            if self.first_turn_args is None:
                self.first_turn_args = self.last_turn_args.copy()

            response = get_response_obj(
                [get_text_message("partial final")], response_id="resp-partial"
            )
            yield terminal_event_cls(
                type=terminal_event_type,
                response=response,
                sequence_number=0,
            )

    model = TerminalPayloadFakeModel()
    agent = Agent(name="test", model=model)

    result = Runner.run_streamed(agent, input="test")
    stream_events: list[StreamEvent] = []
    with pytest.raises(ModelBehaviorError, match=terminal_event_type):
        async for event in result.stream_events():
            stream_events.append(event)

    assert len(stream_events) == 2
    assert isinstance(stream_events[0], AgentUpdatedStreamEvent)
    assert isinstance(stream_events[1], RawResponsesStreamEvent)
    assert stream_events[1].data.type == terminal_event_type
    assert result.final_output is None
    assert result.raw_responses == []


@pytest.mark.asyncio
async def test_streamed_run_rejects_response_error_terminal_event() -> None:
    class TerminalErrorFakeModel(FakeModel):
        async def stream_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            self.last_turn_args = {
                "system_instructions": system_instructions,
                "input": input,
                "model_settings": model_settings,
                "tools": tools,
                "output_schema": output_schema,
                "previous_response_id": previous_response_id,
                "conversation_id": conversation_id,
            }
            if self.first_turn_args is None:
                self.first_turn_args = self.last_turn_args.copy()

            yield ResponseErrorEvent(
                type="error",
                code="invalid_request_error",
                message="bad request",
                param=None,
                sequence_number=0,
            )

    model = TerminalErrorFakeModel()
    agent = Agent(name="test", model=model)

    result = Runner.run_streamed(agent, input="test")
    stream_events: list[StreamEvent] = []
    with pytest.raises(ModelBehaviorError, match="error"):
        async for event in result.stream_events():
            stream_events.append(event)

    assert len(stream_events) == 2
    assert isinstance(stream_events[0], AgentUpdatedStreamEvent)
    assert isinstance(stream_events[1], RawResponsesStreamEvent)
    assert stream_events[1].data.type == "error"
    assert stream_events[1].data.code == "invalid_request_error"
    assert stream_events[1].data.message == "bad request"
    assert result.final_output is None
    assert result.raw_responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_raw_usage", [None, False, True])
async def test_streamed_run_exposes_request_id_on_raw_responses(
    preserve_raw_usage: bool | None,
) -> None:
    class RequestIdTerminalFakeModel(FakeModel):
        async def stream_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            response = get_response_obj(
                [get_text_message("partial final")], response_id="resp-partial"
            )
            response._request_id = "req_streamed_result_123"
            _attach_raw_usage_snapshot(
                response,
                {"input_tokens": 3, "input_tokens_details": {"cached_tokens": 0}},
            )
            yield ResponseCompletedEvent(
                type="response.completed",
                response=response,
                sequence_number=0,
            )

    model = RequestIdTerminalFakeModel()
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(preserve_raw_usage=preserve_raw_usage),
    )

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert len(result.raw_responses) == 1
    assert result.raw_responses[0].request_id == "req_streamed_result_123"
    assert result.raw_responses[0].raw_usage == (
        {
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
        }
        if preserve_raw_usage is True
        else None
    )


@pytest.mark.asyncio
async def test_streamed_run_preserves_request_usage_entries_after_retry() -> None:
    model = FakeModel()
    model.set_hardcoded_usage(
        Usage(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
    )
    model.add_multiple_turn_outputs(
        [
            APIConnectionError(
                message="connection error",
                request=httpx.Request("POST", "https://example.com"),
            ),
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            )
        ),
    )

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    usage = result.context_wrapper.usage
    assert usage.requests == 2
    assert len(usage.request_usage_entries) == 2
    assert usage.request_usage_entries[0].total_tokens == 0
    assert usage.request_usage_entries[1].input_tokens == 10
    assert usage.request_usage_entries[1].output_tokens == 5
    assert usage.request_usage_entries[1].total_tokens == 15


class _RetryThenMissingUsageModel(Model):
    """Stream a successful retry whose terminal Response omits usage data."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise APIConnectionError(
                message="connection error",
                request=httpx.Request("POST", "https://example.com"),
            )
        return ModelResponse(
            output=[get_text_message("done")],
            usage=Usage(requests=1),
            response_id="resp-missing-usage",
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        self.calls += 1
        if self.calls == 1:
            raise APIConnectionError(
                message="connection error",
                request=httpx.Request("POST", "https://example.com"),
            )
        response = get_response_obj([get_text_message("done")])
        response.usage = None
        yield ResponseCompletedEvent(
            type="response.completed",
            response=response,
            sequence_number=0,
        )


@pytest.mark.asyncio
async def test_streamed_run_counts_retry_attempts_when_terminal_usage_missing() -> None:
    """Retry accounting must survive successful streams that omit Response.usage.

    Non-OpenAI chat-completions adapters (e.g. LiteLLM) can complete a stream without a usage
    chunk, leaving ``Response.usage`` as ``None``. Failed retry attempts must still be counted,
    matching the non-streaming ``apply_retry_attempt_usage`` path.
    """

    model = _RetryThenMissingUsageModel()
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            )
        ),
    )

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    usage = result.context_wrapper.usage
    assert model.calls == 2
    assert usage.requests == 2
    assert len(usage.request_usage_entries) == 2
    assert usage.request_usage_entries[0].total_tokens == 0
    assert usage.request_usage_entries[1].total_tokens == 0


@pytest.mark.asyncio
async def test_streamed_model_retry_does_not_rewind_committed_session_input() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            APIConnectionError(
                message="connection error",
                request=httpx.Request("POST", "https://example.com"),
            ),
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            )
        ),
    )
    session = CountingSession(history=[get_text_input_item("previous")])

    result = Runner.run_streamed(agent, input="test", session=session)
    async for _ in result.stream_events():
        pass

    saved_items = await session.get_items()
    assert [item.get("role") for item in saved_items] == ["user", "user", "assistant"]
    assert session.pop_calls == 0


@pytest.mark.asyncio
async def test_streamed_run_preserves_request_usage_entries_after_conversation_locked_retry() -> (
    None
):
    model = FakeModel()
    model.set_hardcoded_usage(
        Usage(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
    )
    model.add_multiple_turn_outputs(
        [
            _conversation_locked_error(),
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                policy=retry_policies.network_error(),
            )
        ),
    )

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    usage = result.context_wrapper.usage
    assert usage.requests == 2
    assert len(usage.request_usage_entries) == 2
    assert usage.request_usage_entries[0].total_tokens == 0
    assert usage.request_usage_entries[1].input_tokens == 10
    assert usage.request_usage_entries[1].output_tokens == 5
    assert usage.request_usage_entries[1].total_tokens == 15


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event_type", ["response.incomplete", "response.failed"])
async def test_streamed_run_rejects_failed_terminal_response_payload_events_from_ws_model(
    monkeypatch, terminal_event_type: str
) -> None:
    class DummyWSConnection:
        def __init__(self, frames: list[str]):
            self._frames = frames
            self.close_code: int | None = None

        async def send(self, payload: str) -> None:
            return None

        async def recv(self) -> str:
            if not self._frames:
                raise RuntimeError("No more websocket frames configured")
            return self._frames.pop(0)

        async def close(self) -> None:
            if self.close_code is None:
                self.close_code = 1000

    class DummyWSClient:
        def __init__(self) -> None:
            self.base_url = httpx.URL("https://api.openai.com/v1/")
            self.websocket_base_url = None
            self.default_query: dict[str, Any] = {}
            self.default_headers = {
                "Authorization": "Bearer test-key",
                "User-Agent": "AsyncOpenAI/Python test",
            }
            self.timeout: Any = None

        async def _refresh_api_key(self) -> None:
            return None

    ws = DummyWSConnection([_ws_terminal_response_frame(terminal_event_type, "resp-ws", 1)])
    model = OpenAIResponsesWSModel(model="gpt-4", openai_client=DummyWSClient())  # type: ignore[arg-type]

    async def fake_open(
        _ws_url: str,
        _headers: dict[str, str],
        *,
        connect_timeout: float | None = None,
    ) -> DummyWSConnection:
        return ws

    monkeypatch.setattr(model, "_open_websocket_connection", fake_open)

    agent = Agent(name="test", model=model)
    result = Runner.run_streamed(agent, input="test")
    stream_events: list[StreamEvent] = []
    with pytest.raises(ModelBehaviorError, match=terminal_event_type):
        async for event in result.stream_events():
            stream_events.append(event)

    assert len(stream_events) == 2
    assert isinstance(stream_events[0], AgentUpdatedStreamEvent)
    assert isinstance(stream_events[1], RawResponsesStreamEvent)
    assert stream_events[1].data.type == terminal_event_type
    assert result.final_output is None
    assert result.raw_responses == []


@pytest.mark.asyncio
async def test_subsequent_runs():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )
    model.set_next_output([get_text_message("third")])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("fourth")])

    result = Runner.run_streamed(agent, input=result.to_input_list())
    async for _ in result.stream_events():
        pass

    assert len(result.input) == 2, f"should have previous input but got {result.input}"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "fourth"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert result.raw_responses[0].output == [get_text_message("fourth")]
    assert result.last_agent == agent
    assert len(result.to_input_list()) == 3, "should have original input and generated items"


@pytest.mark.asyncio
async def test_tool_call_runs():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("foo", json.dumps({"a": "b"}))],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    assert len(result.raw_responses) == 2, (
        "should have two responses: the first which produces a tool call, and the second which"
        "handles the tool result"
    )

    assert len(result.to_input_list()) == 5, (
        "should have five inputs: the original input, the message, the tool call, the tool result "
        "and the done message"
    )


@pytest.mark.asyncio
async def test_streamed_parallel_tool_call_with_cancelled_sibling_reaches_final_output() -> None:
    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[
            function_tool(_ok_tool, name_override="ok_tool"),
            function_tool(_cancel_tool, name_override="cancel_tool"),
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("ok_tool", "{}", call_id="call_ok"),
                get_function_tool_call("cancel_tool", "{}", call_id="call_cancel"),
            ],
            [get_text_message("final answer")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message")
    await consume_stream(result)

    assert result.final_output == "final answer"
    assert len(result.raw_responses) == 2

    second_turn_input = cast(list[dict[str, Any]], model.last_turn_args["input"])
    tool_outputs = [
        item for item in second_turn_input if item.get("type") == "function_call_output"
    ]
    assert tool_outputs == [
        {"call_id": "call_ok", "output": "ok", "type": "function_call_output"},
        {
            "call_id": "call_cancel",
            "output": (
                "An error occurred while running the tool. Please try again. Error: tool-cancelled"
            ),
            "type": "function_call_output",
        },
    ]


@pytest.mark.asyncio
async def test_streamed_single_tool_call_with_cancelled_tool_reaches_final_output() -> None:
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[function_tool(_cancel_tool, name_override="cancel_tool")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("cancel_tool", "{}", call_id="call_cancel")],
            [get_text_message("final answer")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message")
    await consume_stream(result)

    assert result.final_output == "final answer"
    assert len(result.raw_responses) == 2

    second_turn_input = cast(list[dict[str, Any]], model.last_turn_args["input"])
    tool_outputs = [
        item for item in second_turn_input if item.get("type") == "function_call_output"
    ]
    assert tool_outputs == [
        {
            "call_id": "call_cancel",
            "output": (
                "An error occurred while running the tool. Please try again. Error: tool-cancelled"
            ),
            "type": "function_call_output",
        },
    ]


@pytest.mark.asyncio
async def test_streamed_reasoning_item_id_policy_omits_follow_up_reasoning_ids() -> None:
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            [
                ResponseReasoningItem(
                    id="rs_stream",
                    type="reasoning",
                    summary=[Summary(text="Thinking...", type="summary_text")],
                ),
                get_function_tool_call("foo", json.dumps({"a": "b"}), call_id="call_stream"),
            ],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(
        agent,
        input="hello",
        run_config=RunConfig(reasoning_item_id_policy="omit"),
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    second_request_reasoning = _find_reasoning_input_item(model.last_turn_args.get("input"))
    assert second_request_reasoning is not None
    assert "id" not in second_request_reasoning

    history_reasoning = _find_reasoning_input_item(result.to_input_list())
    assert history_reasoning is not None
    assert "id" not in history_reasoning


class _StreamedRevokedReasoningIdModel(FakeModel):
    """FakeModel that 404s like the Responses API when a revoked reasoning ID is replayed."""

    def __init__(self) -> None:
        super().__init__()
        self.revoked_reasoning_ids: set[str] = set()

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[TResponseStreamEvent]:
        if isinstance(input, list):
            for item in input:
                if not isinstance(item, dict) or item.get("type") != "reasoning":
                    continue
                item_id = item.get("id")
                if item_id in self.revoked_reasoning_ids:
                    message = f"Item with id '{item_id}' not found."
                    body = {"error": {"message": message, "type": "invalid_request_error"}}
                    raise NotFoundError(
                        message,
                        response=httpx.Response(
                            404,
                            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
                            json=body,
                        ),
                        body=body,
                    )
        return super().stream_response(system_instructions, input, *args, **kwargs)


@pytest.mark.asyncio
async def test_streamed_omit_policy_strips_reasoning_ids_already_stored_in_the_session() -> None:
    """Adopting `omit` must also cover reasoning IDs a session recorded before it was set.

    Streaming counterpart of the non-streamed regression test for
    https://github.com/openai/openai-agents-python/issues/2020.
    """
    model = _StreamedRevokedReasoningIdModel()
    specialist = Agent(name="specialist", model=model)
    triage = Agent(name="triage", model=model, handoffs=[specialist])
    session = SQLiteSession("issue-2020-streamed")

    # Turn 1 predates the mitigation, so the session records the reasoning ID.
    model.add_multiple_turn_outputs(
        [
            [
                ResponseReasoningItem(id="rs_triage", type="reasoning", summary=[]),
                get_handoff_tool_call(specialist),
            ],
            [get_text_message("handled")],
        ]
    )
    first = Runner.run_streamed(triage, input="hello", session=session)
    async for _ in first.stream_events():
        pass
    assert first.final_output == "handled"
    stored_reasoning = _find_reasoning_input_item(await session.get_items())
    assert stored_reasoning is not None
    assert stored_reasoning.get("id") == "rs_triage"

    # The server no longer resolves that reasoning item.
    model.revoked_reasoning_ids.add("rs_triage")

    # Turn 2 opts into the documented mitigation for this failure.
    model.add_multiple_turn_outputs([[get_text_message("done")]])
    second = Runner.run_streamed(
        triage,
        input="anything else?",
        session=session,
        run_config=RunConfig(reasoning_item_id_policy="omit"),
    )
    async for _ in second.stream_events():
        pass

    assert second.final_output == "done"
    replayed_reasoning = _find_reasoning_input_item(model.last_turn_args.get("input"))
    assert replayed_reasoning is not None
    assert "id" not in replayed_reasoning


@pytest.mark.asyncio
async def test_streamed_run_again_persists_tool_items_to_session():
    model = FakeModel()
    call_id = "call-session-run-again"
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )
    session = SimpleListSession()

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id=call_id)],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message", session=session)
    await consume_stream(result)

    saved_items = await session.get_items()
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == call_id
        for item in saved_items
    )
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == call_id
        for item in saved_items
    )


@pytest.mark.asyncio
async def test_handoffs():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )
    agent_2 = Agent(
        name="test",
        model=model,
    )
    agent_3 = Agent(
        name="test",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent_3, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    assert len(result.raw_responses) == 3, "should have three model responses"
    assert len(result.to_input_list()) == 7, (
        "should have 7 inputs: summary message, tool call, tool result, message, handoff, "
        "handoff result, and done message"
    )
    assert result.last_agent == agent_1, "should have handed off to agent_1"


class Foo(TypedDict):
    bar: str


@pytest.mark.asyncio
async def test_structured_output():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("bar", "bar_result")],
        output_type=Foo,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "foo_result")],
        handoffs=[agent_1],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [
                get_function_tool_call(
                    "foo",
                    json.dumps({"bar": "baz"}),
                    call_id="call_foo",
                )
            ],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: tool call with preamble message
            [
                get_text_message(json.dumps(Foo(bar="preamble"))),
                get_function_tool_call(
                    "bar",
                    json.dumps({"bar": "baz"}),
                    call_id="call_bar",
                ),
            ],
            # Fourth turn: structured output
            [get_final_output_message(json.dumps(Foo(bar="baz")))],
        ]
    )

    result = Runner.run_streamed(
        agent_2,
        input=[
            get_text_input_item("user_message"),
            get_text_input_item("another_message"),
        ],
        run_config=RunConfig(nest_handoff_history=True),
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == Foo(bar="baz")
    assert len(result.raw_responses) == 4, "should have four model responses"
    assert len(result.to_input_list()) == 11, (
        "should preserve ordered history segments plus function calls, messages, handoff items, "
        "and the final output without replaying the carried-forward message twice"
    )
    assert len(result.to_input_list(mode="normalized")) == 7, (
        "should have normalized replay input: conversation summary, carried-forward message, "
        "handoff summary, preamble message, tool call, tool call result, final output"
    )

    assert result.last_agent == agent_1, "should have handed off to agent_1"
    assert result.final_output == Foo(bar="baz"), "should have structured output"


def remove_new_items(handoff_input_data: HandoffInputData) -> HandoffInputData:
    return HandoffInputData(
        input_history=handoff_input_data.input_history,
        pre_handoff_items=(),
        new_items=(),
        run_context=handoff_input_data.run_context,
    )


@pytest.mark.asyncio
async def test_handoff_filters():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )
    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                input_filter=remove_new_items,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"
    assert len(result.raw_responses) == 2, "should have two model responses"
    assert len(result.to_input_list()) == 2, (
        "should only have 2 inputs: orig input and last message"
    )


@pytest.mark.asyncio
async def test_streamed_nested_handoff_filters_reasoning_items_from_model_input():
    model = FakeModel()
    delegate = Agent(
        name="delegate",
        model=model,
    )
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
    )

    model.add_multiple_turn_outputs(
        [
            [
                ResponseReasoningItem(
                    id="reasoning_1",
                    type="reasoning",
                    summary=[Summary(text="Thinking about a handoff.", type="summary_text")],
                ),
                get_handoff_tool_call(delegate),
            ],
            [get_text_message("done")],
        ]
    )

    captured_inputs: list[list[dict[str, Any]]] = []

    def capture_model_input(data):
        if isinstance(data.model_data.input, list):
            captured_inputs.append(
                [item for item in data.model_data.input if isinstance(item, dict)]
            )
        return data.model_data

    result = Runner.run_streamed(
        triage,
        input="user_message",
        run_config=RunConfig(
            nest_handoff_history=True,
            call_model_input_filter=capture_model_input,
        ),
    )
    await consume_stream(result)

    assert result.final_output == "done"
    assert len(captured_inputs) >= 2
    handoff_input = captured_inputs[1]
    handoff_input_types = [
        item["type"] for item in handoff_input if isinstance(item.get("type"), str)
    ]
    assert "reasoning" not in handoff_input_types


@pytest.mark.asyncio
async def test_async_input_filter_supported():
    # DO NOT rename this without updating pyproject.toml

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    async def async_input_filter(data: HandoffInputData) -> HandoffInputData:
        return data  # pragma: no cover

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                input_filter=async_input_filter,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass


@pytest.mark.asyncio
async def test_invalid_input_filter_fails():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    def invalid_input_filter(data: HandoffInputData) -> HandoffInputData:
        # Purposely returning a string to simulate invalid output
        return "foo"  # type: ignore

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                input_filter=invalid_input_filter,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    with pytest.raises(UserError):
        result = Runner.run_streamed(agent_2, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_non_callable_input_filter_causes_error():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                # Purposely ignoring the type error here to simulate invalid input
                input_filter="foo",  # type: ignore
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    with pytest.raises(UserError):
        result = Runner.run_streamed(agent_2, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_handoff_on_input():
    call_output: str | None = None

    def on_input(_ctx: RunContextWrapper[Any], data: Foo) -> None:
        nonlocal call_output
        call_output = data["bar"]

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                on_handoff=on_input,
                input_type=Foo,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_text_message("2"),
                get_handoff_tool_call(agent_1, args=json.dumps(Foo(bar="test_input"))),
            ],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"

    assert call_output == "test_input", "should have called the handoff with the correct input"


@pytest.mark.asyncio
async def test_async_handoff_on_input():
    call_output: str | None = None

    async def on_input(_ctx: RunContextWrapper[Any], data: Foo) -> None:
        nonlocal call_output
        call_output = data["bar"]

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                on_handoff=on_input,
                input_type=Foo,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_text_message("2"),
                get_handoff_tool_call(agent_1, args=json.dumps(Foo(bar="test_input"))),
            ],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"

    assert call_output == "test_input", "should have called the handoff with the correct input"


@pytest.mark.asyncio
async def test_input_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    agent = Agent(
        name="test",
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
        model=FakeModel(),
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted"),
    [(True, False), (False, True), (False, False)],
    ids=["model_redacted", "tool_redacted", "diagnostic"],
)
@pytest.mark.asyncio
async def test_streamed_finalizer_failure_follows_both_data_policies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    model_redacted: bool,
    tool_redacted: bool,
) -> None:
    async def safe_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        _ = context, agent, input
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    error = RuntimeError("SECRET_STREAM_FINALIZER_ERROR")

    async def fail_finalizer(_result: Any) -> bool:
        raise error

    monkeypatch.setattr(
        run_loop,
        "input_guardrail_tripwire_triggered_for_stream",
        fail_finalizer,
    )
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    agent_name = "SECRET_STREAM_AGENT_NAME"
    agent = Agent(
        name=agent_name,
        input_guardrails=[InputGuardrail(guardrail_function=safe_guardrail)],
        model=FakeModel(initial_output=[get_text_message("done")]),
    )

    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass

    assert result.final_output == "done"
    record = next(
        record
        for record in caplog.records
        if "Error finalizing streamed result" in record.getMessage()
    )
    redacted = model_redacted or tool_redacted
    if redacted:
        assert record.msg == "%s"
        assert record.args == ("Error finalizing streamed result",)
        assert record.exc_info is None
        assert record.exc_text is None
        assert "openai_agents_diagnostic_context" not in record.__dict__
        rendered = logging.Formatter().format(record)
        assert agent_name not in rendered
        assert "SECRET_STREAM_FINALIZER_ERROR" not in rendered
    else:
        context = record.__dict__["openai_agents_diagnostic_context"]
        assert context == {"agent_name": agent_name}
        assert record.exc_info is not None
        assert record.exc_info[1] is error
        assert "SECRET_STREAM_FINALIZER_ERROR" in logging.Formatter().format(record)


@pytest.mark.asyncio
async def test_input_guardrail_streamed_does_not_save_assistant_message_to_session():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0.01)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_input_guardrail_streamed_persists_user_input_for_sequential_guardrail():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[
            InputGuardrail(guardrail_function=guardrail_function, run_in_parallel=False)
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_input_guardrail_streamed_persists_user_input_for_async_sequential_guardrail():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[
            InputGuardrail(guardrail_function=guardrail_function, run_in_parallel=False)
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_stream_input_persistence_strips_ids_for_openai_conversation_session():
    class DummyOpenAIConversationsSession(OpenAIConversationsSession):
        def __init__(self) -> None:
            self.saved: list[list[TResponseInputItem]] = []

        async def _get_session_id(self) -> str:
            return "conv_test"

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            for item in items:
                if isinstance(item, dict):
                    assert "id" not in item, "IDs should be stripped before saving"
                    assert "provider_data" not in item, (
                        "provider_data should be stripped before saving"
                    )
            self.saved.append(items)

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            return []

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            return None

    session = DummyOpenAIConversationsSession()

    model = FakeModel()
    model.set_next_output([get_text_message("ok")])

    agent = Agent(
        name="test",
        model=model,
    )

    run_config = RunConfig(session_input_callback=lambda existing, new: existing + new)

    input_items = [
        cast(
            TResponseInputItem,
            {
                "id": "message-1",
                "type": "message",
                "role": "user",
                "content": "hello",
                "provider_data": {"model": "litellm/test"},
            },
        )
    ]

    result = Runner.run_streamed(agent, input=input_items, session=session, run_config=run_config)
    async for _ in result.stream_events():
        pass

    assert session.saved, "input items should be persisted via save_result_to_session"
    assert len(session.saved[0]) == 1
    saved_item = session.saved[0][0]
    assert isinstance(saved_item, dict)
    assert "id" not in saved_item, "saved input items should not include IDs"


@pytest.mark.asyncio
async def test_stream_input_persistence_saves_only_new_turn_input(monkeypatch: pytest.MonkeyPatch):
    session = SimpleListSession()
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first")],
            [get_text_message("second")],
        ]
    )
    agent = Agent(name="test", model=model)

    from agents.run_internal import session_persistence as sp

    real_save_result = sp.save_result_to_session
    input_saves: list[list[TResponseInputItem]] = []

    async def save_wrapper(
        sess: Any,
        original_input: Any,
        new_items: list[RunItem],
        run_state: Any = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(original_input, list) and original_input:
            input_saves.append(list(original_input))
        await real_save_result(sess, original_input, new_items, run_state, **kwargs)

    monkeypatch.setattr(
        "agents.run_internal.session_persistence.save_result_to_session", save_wrapper
    )
    monkeypatch.setattr("agents.run_internal.run_loop.save_result_to_session", save_wrapper)

    run_config = RunConfig(session_input_callback=lambda existing, new: existing + new)

    first = Runner.run_streamed(
        agent, input=[get_text_input_item("hello")], session=session, run_config=run_config
    )
    async for _ in first.stream_events():
        pass

    second = Runner.run_streamed(
        agent, input=[get_text_input_item("next")], session=session, run_config=run_config
    )
    async for _ in second.stream_events():
        pass

    assert len(input_saves) == 2, "each turn should persist only the turn input once"
    assert all(len(saved) == 1 for saved in input_saves), (
        "each persisted input should contain only the new turn items"
    )
    first_saved = input_saves[0][0]
    second_saved = input_saves[1][0]
    assert isinstance(first_saved, dict) and first_saved.get("content") == "hello"
    assert isinstance(second_saved, dict) and second_saved.get("content") == "next"


@pytest.mark.asyncio
async def test_slow_input_guardrail_still_raises_exception_streamed():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        # Simulate a slow guardrail that completes after model streaming ends.
        await asyncio.sleep(0.05)
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel()
    # Ensure the model finishes streaming quickly.
    model.set_next_output([get_text_message("ok")])

    agent = Agent(
        name="test",
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    # Even though the guardrail is slower than the model stream, the exception should still raise.
    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    with pytest.raises(OutputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_raises_from_run_loop_task_before_stream_consumption():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    result = Runner.run_streamed(agent, input="user_message")

    assert result.run_loop_task is not None
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await result.run_loop_task

    assert result.final_output is None
    assert result.is_complete is True


@pytest.mark.asyncio
async def test_output_guardrail_exception_raises_from_run_loop_task_before_stream_consumption():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        raise RuntimeError("guardrail failed")

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    result = Runner.run_streamed(agent, input="user_message")

    assert result.run_loop_task is not None
    with pytest.raises(RuntimeError, match="guardrail failed"):
        await result.run_loop_task

    assert result.final_output is None
    assert result.is_complete is True


@pytest.mark.asyncio
async def test_run_input_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    agent = Agent(
        name="test",
        model=FakeModel(),
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(
            agent,
            input="user_message",
            run_config=RunConfig(
                input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)]
            ),
        )
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_run_output_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        model=model,
    )

    with pytest.raises(OutputGuardrailTripwireTriggered):
        result = Runner.run_streamed(
            agent,
            input="user_message",
            run_config=RunConfig(
                output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)]
            ),
        )
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_streaming_events():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("bar", "bar_result")],
        output_type=Foo,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "foo_result")],
        handoffs=[agent_1],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [
                get_function_tool_call(
                    "foo",
                    json.dumps({"bar": "baz"}),
                    call_id="call_foo",
                )
            ],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: tool call
            [
                get_function_tool_call(
                    "bar",
                    json.dumps({"bar": "baz"}),
                    call_id="call_bar",
                )
            ],
            # Fourth turn: structured output
            [get_final_output_message(json.dumps(Foo(bar="baz")))],
        ]
    )

    # event_type: (count, event)
    event_counts: dict[str, int] = {}
    item_data: list[RunItem] = []
    agent_data: list[AgentUpdatedStreamEvent] = []

    result = Runner.run_streamed(
        agent_2,
        input=[
            get_text_input_item("user_message"),
            get_text_input_item("another_message"),
        ],
        run_config=RunConfig(nest_handoff_history=True),
    )
    async for event in result.stream_events():
        event_counts[event.type] = event_counts.get(event.type, 0) + 1
        if event.type == "run_item_stream_event":
            item_data.append(event.item)
        elif event.type == "agent_updated_stream_event":
            agent_data.append(event)

    assert result.final_output == Foo(bar="baz")
    assert len(result.raw_responses) == 4, "should have four model responses"
    assert len(result.to_input_list()) == 10, (
        "should preserve ordered history segments plus function calls, messages, handoff items, "
        "and the final output without replaying the carried-forward message twice"
    )
    assert len(result.to_input_list(mode="normalized")) == 6, (
        "should have normalized replay input: conversation summary, carried-forward message, "
        "handoff summary, tool call, tool call result, final output"
    )

    assert result.last_agent == agent_1, "should have handed off to agent_1"
    assert result.final_output == Foo(bar="baz"), "should have structured output"

    # Now lets check the events

    expected_item_type_map = {
        # 2 tool_call_item events:
        #   1. get_function_tool_call("foo", ...)
        #   2. get_function_tool_call("bar", ...)
        # get_handoff_tool_call(agent_1) is only reported as a handoff_call_item.
        "tool_call": 2,
        # Only 2 outputs, handoff tool call doesn't have corresponding tool_call_output event
        "tool_call_output": 2,
        "message": 2,  # get_text_message("a_message") + get_final_output_message(...)
        "handoff": 1,  # get_handoff_tool_call(agent_1)
        "handoff_output": 1,  # handoff_output_item
    }

    total_expected_item_count = sum(expected_item_type_map.values())

    assert event_counts["run_item_stream_event"] == total_expected_item_count, (
        f"Expected {total_expected_item_count} events, got {event_counts['run_item_stream_event']}"
        f"Expected events were: {expected_item_type_map}, got {event_counts}"
    )

    assert len(item_data) == total_expected_item_count, (
        f"should have {total_expected_item_count} run items"
    )
    assert len(agent_data) == 2, "should have 2 agent updated events"
    assert agent_data[0].new_agent == agent_2, "should have started with agent_2"
    assert agent_data[1].new_agent == agent_1, "should have handed off to agent_1"


@pytest.mark.asyncio
async def test_dynamic_tool_addition_run_streamed() -> None:
    model = FakeModel()

    executed: dict[str, bool] = {"called": False}

    agent = Agent(name="test", model=model, tool_use_behavior="run_llm_again")

    @function_tool(name_override="tool2")
    def tool2() -> str:
        executed["called"] = True
        return "result2"

    @function_tool(name_override="add_tool")
    async def add_tool() -> str:
        agent.tools.append(tool2)
        return "added"

    agent.tools.append(add_tool)

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("add_tool", json.dumps({}), call_id="call-add-tool")],
            [get_function_tool_call("tool2", json.dumps({}), call_id="call-tool-two")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="start")
    async for _ in result.stream_events():
        pass

    assert executed["called"] is True
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_stream_step_items_to_queue_handles_tool_approval_item():
    """Test that stream_step_items_to_queue handles ToolApprovalItem."""
    _, agent = make_model_and_agent(name="test")
    tool_call = get_function_tool_call("test_tool", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] = asyncio.Queue()

    # ToolApprovalItem should not be streamed
    run_loop.stream_step_items_to_queue([approval_item], queue)

    # Queue should be empty since ToolApprovalItem is not streamed
    assert queue.empty()


@pytest.mark.asyncio
async def test_streaming_hitl_resume_with_approved_tools():
    """Test resuming streaming run from RunState with approved tools executes them."""
    tool_called = False

    async def test_tool() -> str:
        nonlocal tool_called
        tool_called = True
        return "tool_result"

    # Create a tool that requires approval
    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])

    # First run - tool call that requires approval
    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool")
    await consume_stream(first)

    # Resume from state - should execute approved tool
    result2 = await resume_streamed_after_first_approval(agent, first)

    # Tool should have been called
    assert tool_called is True
    assert result2.final_output == "done"


@pytest.mark.asyncio
async def test_streaming_resume_with_session_does_not_duplicate_items():
    """Ensure session persistence does not duplicate tool items after streaming resume."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)
    assert resumed.final_output == "done"

    saved_items = await session.get_items()
    call_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == "call-resume"
    )
    output_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
    )

    assert call_count == 1
    assert output_count == 1


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.parametrize("tripwire", [False, True], ids=["passes", "trips"])
@pytest.mark.asyncio
async def test_resumed_approved_tool_final_persists_call_output_before_output_guardrails(
    mode: str,
    tripwire: bool,
) -> None:
    guardrail_state = {"tripwire": tripwire}

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=guardrail_state["tripwire"],
        )

    model = FakeModel()
    model.set_next_output([get_function_tool_call("approval_tool", "{}", call_id="call-approved")])
    agent = Agent(
        name="test",
        model=model,
        tools=[approval_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    async def run_once(input_value: Any) -> Any:
        if mode == "non_streamed":
            return await Runner.run(agent, input_value, session=session)
        result = Runner.run_streamed(agent, input_value, session=session)
        await consume_stream(result)
        return result

    first = await run_once("Use approval_tool")
    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    if tripwire:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await run_once(state)
    else:
        resumed = await run_once(state)
        assert resumed.final_output == "approved-result"

    saved_items = await session.get_items()
    saved_types = [
        item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)
    ]
    assert saved_types == ["user", "function_call", "function_call_output"]
    saved_tool_items = [
        item
        for item in saved_items
        if isinstance(item, dict) and item.get("type") in {"function_call", "function_call_output"}
    ]
    assert [(item.get("type"), item.get("call_id")) for item in saved_tool_items] == [
        ("function_call", "call-approved"),
        ("function_call_output", "call-approved"),
    ]
    assert saved_tool_items[1].get("output") == "approved-result"

    if tripwire:
        guardrail_state["tripwire"] = False
        model.set_next_output([get_text_message("done")])
        next_result = await run_once("Continue")
        assert next_result.final_output == "done"

        model_input = model.last_turn_args["input"]
        assert isinstance(model_input, list)
        replayed_tool_items = [
            item
            for item in model_input
            if isinstance(item, dict)
            and item.get("type") in {"function_call", "function_call_output"}
        ]
        assert [(item.get("type"), item.get("call_id")) for item in replayed_tool_items] == [
            ("function_call", "call-approved"),
            ("function_call_output", "call-approved"),
        ]
        assert replayed_tool_items[1].get("output") == "approved-result"


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_stop_on_first_tool_final_persists_committed_tool_items_on_tripwire(
    mode: str,
) -> None:
    """A blocked final output must not discard the session record of a tool that already ran."""

    calls: list[str] = []

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        calls.append("ran")
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = FakeModel()
    model.set_next_output([get_function_tool_call("commit_tool", "{}", call_id="call-committed")])
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    with pytest.raises(OutputGuardrailTripwireTriggered):
        if mode == "non_streamed":
            await Runner.run(agent, "Use commit_tool", session=session)
        else:
            result = Runner.run_streamed(agent, "Use commit_tool", session=session)
            await consume_stream(result)

    assert calls == ["ran"], "the tool never ran, so the test proves nothing"

    saved_items = await session.get_items()
    saved = [
        (item.get("type") or item.get("role"), item.get("call_id"))
        for item in saved_items
        if isinstance(item, dict)
    ]
    assert saved == [
        ("user", None),
        ("function_call", "call-committed"),
        ("function_call_output", "call-committed"),
    ]

    # The next run must see the completed call instead of re-issuing the same side effect.
    agent.output_guardrails = []
    model.set_next_output([get_text_message("done")])
    if mode == "non_streamed":
        followup: Any = await Runner.run(agent, "Continue", session=session)
    else:
        followup = Runner.run_streamed(agent, "Continue", session=session)
        await consume_stream(followup)
    assert followup.final_output == "done"
    assert calls == ["ran"]

    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    replayed = [
        (item.get("type"), item.get("call_id"))
        for item in model_input
        if isinstance(item, dict) and item.get("type") in {"function_call", "function_call_output"}
    ]
    assert replayed == [
        ("function_call", "call-committed"),
        ("function_call_output", "call-committed"),
    ]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_blocked_message_final_output_is_not_persisted(mode: str) -> None:
    """Control for the committed-tool case: a rejected message is withheld from the session."""

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])
    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    with pytest.raises(OutputGuardrailTripwireTriggered):
        if mode == "non_streamed":
            await Runner.run(agent, "user_message", session=session)
        else:
            result = Runner.run_streamed(agent, "user_message", session=session)
            await consume_stream(result)

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user"]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_blocked_final_persists_tool_items_but_not_the_message(mode: str) -> None:
    """A mixed final turn splits: the tool record is kept, the blocked message is not."""

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("commit_tool", "{}", call_id="call-mixed")],
            [get_text_message("should_not_be_saved")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    with pytest.raises(OutputGuardrailTripwireTriggered):
        if mode == "non_streamed":
            await Runner.run(agent, "Use commit_tool", session=session)
        else:
            result = Runner.run_streamed(agent, "Use commit_tool", session=session)
            await consume_stream(result)

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user", "function_call", "function_call_output"]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.parametrize("tripwire", [False, True], ids=["passes", "trips"])
@pytest.mark.asyncio
async def test_mixed_final_turn_session_order_and_committed_items(
    mode: str,
    tripwire: bool,
) -> None:
    """A final turn holding a message *and* a committed tool call keeps model order when it passes.

    Only the tripwire case may drop anything, and only the undeliverable message. The passing case
    must persist the whole batch in the model's order, so a later run does not replay a reordered
    or truncated history.
    """

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=tripwire)

    model = FakeModel()
    # The message precedes the tool call, so a split save would reorder the persisted turn.
    model.set_next_output(
        [
            get_text_message("assistant-preamble"),
            get_function_tool_call("commit_tool", "{}", call_id="call-mixed"),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    async def run_once() -> Any:
        if mode == "non_streamed":
            return await Runner.run(agent, "Use commit_tool", session=session)
        result = Runner.run_streamed(agent, "Use commit_tool", session=session)
        await consume_stream(result)
        return result

    if tripwire:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await run_once()
    else:
        assert (await run_once()).final_output == "committed-result"

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]

    if tripwire:
        # The undeliverable message is withheld; the tool that already ran is not.
        assert saved == ["user", "function_call", "function_call_output"]
    else:
        assert saved == ["user", "message", "function_call", "function_call_output"]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_failing_output_guardrail_keeps_the_whole_final_turn(
    mode: str,
) -> None:
    """A guardrail *error* is not a tripwire: the completed final turn stays replayable.

    Only a tripwire means the output was judged undeliverable. An ordinary guardrail exception
    leaves the verdict unknown, so the turn must be persisted whole, exactly as the non-streamed
    path does.
    """

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        raise RuntimeError("guardrail failed")

    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("assistant-preamble"),
            get_function_tool_call("commit_tool", "{}", call_id="call-mixed"),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    async def run_once() -> None:
        if mode == "non_streamed":
            await Runner.run(agent, "Use commit_tool", session=session)
        else:
            result = Runner.run_streamed(agent, "Use commit_tool", session=session)
            await consume_stream(result)

    with pytest.raises(RuntimeError, match="guardrail failed"):
        await run_once()

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user", "message", "function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_streamed_session_save_error_takes_precedence_over_output_guardrail_error() -> None:
    guardrail_failed = False
    final_turn_save_attempted = False

    class FailingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[TResponseInputItem]) -> None:
            nonlocal final_turn_save_attempted
            if guardrail_failed:
                final_turn_save_attempted = True
                raise LookupError("session save failed")
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        raise RuntimeError("guardrail failed")

    model = FakeModel()
    model.set_next_output([get_text_message("assistant-preamble")])
    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = FailingFinalTurnSession()
    result = Runner.run_streamed(agent, "Hello", session=session)

    with pytest.raises(LookupError, match="session save failed") as exc_info:
        await consume_stream(result)

    assert final_turn_save_attempted is True
    assert isinstance(exc_info.value.__context__, RuntimeError)
    assert str(exc_info.value.__context__) == "guardrail failed"
    assert result.run_loop_exception is exc_info.value
    assert await session.get_items() == [{"content": "Hello", "role": "user"}]


@pytest.mark.asyncio
async def test_streamed_session_save_cancellation_is_not_a_public_immediate_cancel() -> None:
    guardrail_failed = False

    class CancellingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[TResponseInputItem]) -> None:
            if guardrail_failed:
                raise asyncio.CancelledError("session save cancelled")
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        raise RuntimeError("guardrail failed")

    model = FakeModel(initial_output=[get_text_message("assistant-preamble")])
    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = CancellingFinalTurnSession()
    result = Runner.run_streamed(agent, "Hello", session=session)

    with pytest.raises(asyncio.CancelledError, match="session save cancelled") as exc_info:
        await consume_stream(result)

    assert result._cancel_mode == "none"
    assert result._stored_exception is exc_info.value
    assert isinstance(exc_info.value.__context__, RuntimeError)
    assert str(exc_info.value.__context__) == "guardrail failed"
    assert await session.get_items() == [{"content": "Hello", "role": "user"}]


@pytest.mark.asyncio
async def test_streamed_session_save_direct_base_exception_is_terminal() -> None:
    guardrail_failed = False

    class DirectAbort(BaseException):
        pass

    class AbortingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[TResponseInputItem]) -> None:
            if guardrail_failed:
                raise DirectAbort("session save aborted")
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        raise RuntimeError("guardrail failed")

    agent = Agent(
        name="test",
        model=FakeModel(initial_output=[get_text_message("assistant-preamble")]),
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = AbortingFinalTurnSession()
    result = Runner.run_streamed(agent, "Hello", session=session)

    with pytest.raises(DirectAbort, match="session save aborted") as exc_info:
        await consume_stream(result)

    assert result._stored_exception is exc_info.value
    assert result.run_loop_exception is exc_info.value
    assert await session.get_items() == [{"content": "Hello", "role": "user"}]


@pytest.mark.asyncio
async def test_public_immediate_cancel_during_guardrail_recovery_save_stays_prompt() -> None:
    guardrail_failed = False
    save_started = asyncio.Event()
    save_cancelled = asyncio.Event()
    never_set = asyncio.Event()

    class BlockingFinalTurnSession(SimpleListSession):
        async def add_items(self, items: list[TResponseInputItem]) -> None:
            if guardrail_failed:
                save_started.set()
                try:
                    await never_set.wait()
                finally:
                    save_cancelled.set()
                return
            await super().add_items(items)

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_failed
        guardrail_failed = True
        raise RuntimeError("guardrail failed")

    model = FakeModel(initial_output=[get_text_message("assistant-preamble")])
    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = BlockingFinalTurnSession()
    result = Runner.run_streamed(agent, "Hello", session=session)
    drain_task = asyncio.create_task(consume_stream(result))

    try:
        await asyncio.wait_for(save_started.wait(), timeout=1)
        result.cancel()
        await asyncio.wait_for(drain_task, timeout=1)
    finally:
        if not drain_task.done():
            result.cancel()
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task

    assert save_cancelled.is_set()
    assert result._stored_exception is None
    assert await session.get_items() == [{"content": "Hello", "role": "user"}]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.parametrize("tripwire", [False, True], ids=["passes", "trips"])
@pytest.mark.asyncio
async def test_blocked_tool_final_keeps_reasoning_context_with_the_committed_call(
    mode: str,
    tripwire: bool,
) -> None:
    """A retained tool call keeps the reasoning item it belongs to, in order.

    A reasoning model requires the reasoning item that preceded a function call to accompany that
    call in the next request, so persisting the call/output pair without it leaves an unreplayable
    turn. Asserted on both the session contents and the next run's model input.
    """

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=tripwire)

    model = FakeModel()
    model.set_next_output(
        [
            ResponseReasoningItem(
                id="rs_committed",
                summary=[Summary(text="deciding to call the tool", type="summary_text")],
                type="reasoning",
            ),
            get_function_tool_call("commit_tool", "{}", call_id="call-reasoned"),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    async def run_once(input_value: Any) -> Any:
        if mode == "non_streamed":
            return await Runner.run(agent, input_value, session=session)
        result = Runner.run_streamed(agent, input_value, session=session)
        await consume_stream(result)
        return result

    if tripwire:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await run_once("Use commit_tool")
    else:
        assert (await run_once("Use commit_tool")).final_output == "committed-result"

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user", "reasoning", "function_call", "function_call_output"]

    # The reasoning/call/output group has to reach the next request in that order.
    agent.output_guardrails = []
    model.set_next_output([get_text_message("done")])
    followup = await run_once("Continue")
    assert followup.final_output == "done"

    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    replayed = [
        item.get("type")
        for item in model_input
        if isinstance(item, dict)
        and item.get("type") in {"reasoning", "function_call", "function_call_output"}
    ]
    assert replayed == ["reasoning", "function_call", "function_call_output"]


@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_blocked_tool_final_drops_reasoning_tied_to_the_rejected_message(
    mode: str,
) -> None:
    """Only the reasoning tied to a retained call survives; the message's reasoning goes with it.

    The turn is `reasoning_for_message -> message -> reasoning_for_call -> function_call`. A
    reasoning item belongs to the next non-reasoning item, so retaining every reasoning item
    whenever the turn happens to contain a tool call would leave the rejected message's reasoning
    dangling in the next request.

    """

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        return "committed-result"

    def output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = FakeModel()
    model.set_next_output(
        [
            ResponseReasoningItem(
                id="rs_rejected",
                summary=[Summary(text="drafting the message", type="summary_text")],
                type="reasoning",
            ),
            get_text_message("rejected-preamble"),
            ResponseReasoningItem(
                id="rs_committed",
                summary=[Summary(text="deciding to call the tool", type="summary_text")],
                type="reasoning",
            ),
            get_function_tool_call("commit_tool", "{}", call_id="call-reasoned"),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )
    session = SimpleListSession()

    async def run_once(input_value: Any) -> Any:
        if mode == "non_streamed":
            return await Runner.run(agent, input_value, session=session)
        result = Runner.run_streamed(agent, input_value, session=session)
        await consume_stream(result)
        return result

    with pytest.raises(OutputGuardrailTripwireTriggered):
        await run_once("Use commit_tool")

    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user", "reasoning", "function_call", "function_call_output"]

    saved_reasoning_ids = [
        item.get("id") for item in saved_items if isinstance(item, dict) and item.get("id")
    ]
    assert "rs_committed" in saved_reasoning_ids
    assert "rs_rejected" not in saved_reasoning_ids, (
        "reasoning tied to the rejected message must not be persisted"
    )

    # ...and the surviving group still replays in order, with no dangling reasoning item.
    agent.output_guardrails = []
    model.set_next_output([get_text_message("done")])
    followup = await run_once("Continue")
    assert followup.final_output == "done"

    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    replayed = [
        item.get("type")
        for item in model_input
        if isinstance(item, dict)
        and item.get("type") in {"reasoning", "message", "function_call", "function_call_output"}
    ]
    assert replayed == ["reasoning", "function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_streaming_resume_preserves_filtered_model_input_after_handoff():
    model = FakeModel()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "ok"

    delegate = Agent(
        name="delegate",
        model=model,
        tools=[approval_tool],
    )
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
        tools=[get_function_tool("some_function", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "some_function", json.dumps({"a": "b"}), call_id="triage-call"
                )
            ],
            [get_text_message("a_message"), get_handoff_tool_call(delegate)],
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="delegate-call")],
            [get_text_message("done")],
        ]
    )

    model_input_call_ids: list[set[str]] = []
    model_input_output_call_ids: list[set[str]] = []

    def capture_model_input(data):
        call_ids: set[str] = set()
        output_call_ids: set[str] = set()
        for item in data.model_data.input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            if item_type == "function_call":
                call_ids.add(call_id)
            elif item_type == "function_call_output":
                output_call_ids.add(call_id)
        model_input_call_ids.append(call_ids)
        model_input_output_call_ids.append(output_call_ids)
        return data.model_data

    run_config = RunConfig(
        nest_handoff_history=True,
        call_model_input_filter=capture_model_input,
    )

    first = Runner.run_streamed(triage, input="user_message", run_config=run_config)
    await consume_stream(first)
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(triage, state, run_config=run_config)
    await consume_stream(resumed)

    last_call_ids = model_input_call_ids[-1]
    last_output_call_ids = model_input_output_call_ids[-1]
    assert "triage-call" not in last_call_ids
    assert "triage-call" not in last_output_call_ids
    assert "delegate-call" in last_call_ids
    assert "delegate-call" in last_output_call_ids
    assert resumed.final_output == "done"


@pytest.mark.asyncio
async def test_streaming_resume_persists_tool_outputs_on_run_again():
    """Approved tool outputs should be persisted before streaming resumes the next turn."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)

    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)

    saved_items = await session.get_items()
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
        for item in saved_items
    ), "approved tool outputs should be persisted on resume"


@pytest.mark.asyncio
async def test_streaming_resume_carries_persisted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure resumed streaming preserves the persisted count for session saves."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)
    assert first.interruptions

    persisted_count = first._current_turn_persisted_item_count
    assert persisted_count > 0

    state = first.to_state()
    state.approve(first.interruptions[0])

    observed_counts: list[int] = []
    run_loop_any = cast(Any, run_loop)
    real_save_resumed = run_loop_any.save_resumed_turn_items

    async def save_wrapper(
        *,
        session: Any,
        items: list[RunItem],
        persisted_count: int,
        response_id: str | None,
        reasoning_item_id_policy: str | None = None,
        store: bool | None = None,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> int:
        observed_counts.append(persisted_count)
        result = await real_save_resumed(
            session=session,
            items=items,
            persisted_count=persisted_count,
            response_id=response_id,
            reasoning_item_id_policy=reasoning_item_id_policy,
            store=store,
            wrapper=wrapper,
        )
        return int(result)

    monkeypatch.setattr(run_loop_any, "save_resumed_turn_items", save_wrapper)

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)

    assert observed_counts, "expected resumed save to capture persisted count"
    assert all(count == persisted_count for count in observed_counts)


@pytest.mark.asyncio
async def test_streaming_hitl_resume_enforces_max_turns():
    """Test that streamed resumes advance turn counts for max_turns enforcement."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", max_turns=1)
    await consume_stream(first)

    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state)
    with pytest.raises(MaxTurnsExceeded):
        async for _ in resumed.stream_events():
            pass


@pytest.mark.asyncio
async def test_streaming_max_turns_emits_pending_tool_output_events() -> None:
    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool")
    model, agent = make_model_and_agent(name="test", tools=[tool])

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    result = Runner.run_streamed(agent, input="Use test_tool", max_turns=1)
    streamed_item_types: list[str] = []

    with pytest.raises(MaxTurnsExceeded):
        async for event in result.stream_events():
            if event.type == "run_item_stream_event":
                streamed_item_types.append(event.item.type)

    assert "tool_call_item" in streamed_item_types
    assert "tool_call_output_item" in streamed_item_types


@pytest.mark.asyncio
async def test_streaming_non_max_turns_exception_does_not_emit_queued_events() -> None:
    model, agent = make_model_and_agent(name="test")
    model.set_next_output([get_text_message("done")])

    result = Runner.run_streamed(agent, input="hello")
    result.cancel()
    await asyncio.sleep(0)

    while not result._event_queue.empty():
        result._event_queue.get_nowait()
        result._event_queue.task_done()

    result._stored_exception = RuntimeError("guardrail-triggered")
    result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=agent))

    streamed_events: list[StreamEvent] = []
    with pytest.raises(RuntimeError, match="guardrail-triggered"):
        async for event in result.stream_events():
            streamed_events.append(event)

    assert streamed_events == []


@pytest.mark.asyncio
async def test_streaming_hitl_server_conversation_tracker_priming():
    """Test that resuming streaming run from RunState primes server conversation tracker."""
    model, agent = make_model_and_agent(name="test")

    # First run with conversation_id
    model.set_next_output([get_text_message("First response")])
    result1 = Runner.run_streamed(
        agent, input="test", conversation_id="conv123", previous_response_id="resp123"
    )
    await consume_stream(result1)

    # Create state from result
    state = result1.to_state()

    # Resume with same conversation_id - should not duplicate messages
    model.set_next_output([get_text_message("Second response")])
    result2 = Runner.run_streamed(
        agent, state, conversation_id="conv123", previous_response_id="resp123"
    )
    await consume_stream(result2)

    # Should complete successfully without message duplication
    assert result2.final_output == "Second response"
    assert len(result2.new_items) >= 1


def _tool_with_guardrails() -> FunctionTool:
    """Build a function tool guarded by one input and one output tool guardrail."""

    @tool_input_guardrail
    def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

    @tool_output_guardrail
    def record_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

    @function_tool(
        name_override="guarded_tool",
        tool_input_guardrails=[record_input],
        tool_output_guardrails=[record_output],
    )
    def guarded_tool() -> str:
        return "tool-result"

    return guarded_tool


@pytest.mark.asyncio
async def test_streamed_run_reports_tool_guardrail_results():
    """Streamed runs must expose tool guardrail results like non-streamed runs do."""
    model, agent = make_model_and_agent(tools=[_tool_with_guardrails()])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("guarded_tool", "{}", call_id="call_1")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="hello")
    await consume_stream(result)

    assert result.final_output == "done"
    assert len(result.tool_input_guardrail_results) == 1
    assert result.tool_input_guardrail_results[0].output.output_info == "input-checked"
    assert len(result.tool_output_guardrail_results) == 1
    assert result.tool_output_guardrail_results[0].output.output_info == "output-checked"


@pytest.mark.asyncio
async def test_streamed_tool_guardrail_results_match_non_streamed():
    """The same run reports the same tool guardrail results in both execution modes."""

    def _build() -> tuple[FakeModel, Agent[Any]]:
        model, agent = make_model_and_agent(tools=[_tool_with_guardrails()])
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("guarded_tool", "{}", call_id="call_1")],
                [get_function_tool_call("guarded_tool", "{}", call_id="call_2")],
                [get_text_message("done")],
            ]
        )
        return model, agent

    _, non_streamed_agent = _build()
    non_streamed = await Runner.run(non_streamed_agent, input="hello")

    _, streamed_agent = _build()
    streamed = Runner.run_streamed(streamed_agent, input="hello")
    await consume_stream(streamed)

    assert len(non_streamed.tool_input_guardrail_results) == 2
    assert len(non_streamed.tool_output_guardrail_results) == 2
    assert len(streamed.tool_input_guardrail_results) == len(
        non_streamed.tool_input_guardrail_results
    )
    assert len(streamed.tool_output_guardrail_results) == len(
        non_streamed.tool_output_guardrail_results
    )


@pytest.mark.asyncio
async def test_streamed_tool_guardrail_results_survive_handoff():
    """Tool guardrail results from a handoff turn reach the streamed result."""
    model = FakeModel()
    target = Agent(name="target", model=model)
    agent = Agent(
        name="source",
        model=model,
        tools=[_tool_with_guardrails()],
        handoffs=[target],
    )
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("guarded_tool", "{}", call_id="call_1"),
                get_handoff_tool_call(target),
            ],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="hello")
    await consume_stream(result)

    assert result.final_output == "done"
    assert len(result.tool_input_guardrail_results) == 1
    assert len(result.tool_output_guardrail_results) == 1


@pytest.mark.asyncio
async def test_streamed_interruption_reports_tool_guardrail_results():
    """An interrupted streamed turn reports the tool guardrail results it produced."""

    @tool_input_guardrail
    def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

    @function_tool(name_override="plain_tool", tool_input_guardrails=[record_input])
    def plain_tool() -> str:
        return "plain-result"

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved-result"

    model, agent = make_model_and_agent(tools=[plain_tool, approval_tool])
    model.set_next_output(
        [
            get_function_tool_call("plain_tool", "{}", call_id="call_plain"),
            get_function_tool_call("approval_tool", "{}", call_id="call_approval"),
        ]
    )

    result = Runner.run_streamed(agent, input="hello")
    await consume_stream(result)

    assert len(result.interruptions) == 1
    assert len(result.tool_input_guardrail_results) == 1
    assert result.tool_input_guardrail_results[0].output.output_info == "input-checked"


@pytest.mark.asyncio
async def test_streamed_tool_guardrail_results_persist_into_run_state():
    """Tool guardrail results from a streamed run round-trip through RunState."""
    model, agent = make_model_and_agent(tools=[_tool_with_guardrails()])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("guarded_tool", "{}", call_id="call_1")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="hello")
    await consume_stream(result)

    state = result.to_state()
    assert len(state._tool_input_guardrail_results) == 1
    assert len(state._tool_output_guardrail_results) == 1


@pytest.mark.asyncio
async def test_streamed_resume_tool_guardrail_results_match_non_streamed():
    """Resumed-turn accounting stays identical across execution modes.

    Accumulating tool guardrail results for streamed runs must not change how a resumed turn
    reports them, so this pins streamed and non-streamed resumes to the same value rather than
    to a specific count.
    """

    def _build() -> tuple[FakeModel, Agent[Any]]:
        @tool_input_guardrail
        def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

        @tool_output_guardrail
        def record_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

        @function_tool(
            name_override="approval_tool",
            needs_approval=True,
            tool_input_guardrails=[record_input],
            tool_output_guardrails=[record_output],
        )
        def approval_tool() -> str:
            return "approved-result"

        model, agent = make_model_and_agent(tools=[approval_tool])
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("approval_tool", "{}", call_id="call_approval")],
                [get_text_message("done")],
            ]
        )
        return model, agent

    _, non_streamed_agent = _build()
    non_streamed_first = await Runner.run(non_streamed_agent, "hello")
    assert len(non_streamed_first.interruptions) == 1
    non_streamed_state = non_streamed_first.to_state()
    non_streamed_state.approve(non_streamed_first.interruptions[0])
    non_streamed = await Runner.run(non_streamed_agent, non_streamed_state)

    _, streamed_agent = _build()
    streamed_first = Runner.run_streamed(streamed_agent, input="hello")
    await consume_stream(streamed_first)
    assert len(streamed_first.interruptions) == 1
    streamed = await resume_streamed_after_first_approval(streamed_agent, streamed_first)

    assert non_streamed.final_output == "done"
    assert streamed.final_output == "done"
    assert len(streamed.tool_input_guardrail_results) == len(
        non_streamed.tool_input_guardrail_results
    )
    assert len(streamed.tool_output_guardrail_results) == len(
        non_streamed.tool_output_guardrail_results
    )


@pytest.mark.asyncio
async def test_streamed_resume_terminal_turn_reports_tool_guardrail_results():
    """A resumed streamed turn that ends the run reports its tool guardrail results.

    With `tool_use_behavior="stop_on_first_tool"` the approved tool produces the final output
    inside the resumed turn, so the run finalizes from the resume branch rather than from the
    regular turn loop.
    """

    def _build() -> tuple[FakeModel, Agent[Any]]:
        @tool_input_guardrail
        def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

        @tool_output_guardrail
        def record_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

        @function_tool(
            name_override="approval_tool",
            needs_approval=True,
            tool_input_guardrails=[record_input],
            tool_output_guardrails=[record_output],
        )
        def approval_tool() -> str:
            return "approved-result"

        model = FakeModel()
        agent = Agent(
            name="TestAgent",
            model=model,
            tools=[approval_tool],
            tool_use_behavior="stop_on_first_tool",
        )
        model.set_next_output(
            [get_function_tool_call("approval_tool", "{}", call_id="call_approval")]
        )
        return model, agent

    _, streamed_agent = _build()
    streamed_first = Runner.run_streamed(streamed_agent, input="hello")
    await consume_stream(streamed_first)
    assert len(streamed_first.interruptions) == 1
    streamed = await resume_streamed_after_first_approval(streamed_agent, streamed_first)

    assert streamed.final_output == "approved-result"
    assert len(streamed.tool_input_guardrail_results) == 1
    assert streamed.tool_input_guardrail_results[0].output.output_info == "input-checked"
    assert len(streamed.tool_output_guardrail_results) == 1
    assert streamed.tool_output_guardrail_results[0].output.output_info == "output-checked"

    _, non_streamed_agent = _build()
    non_streamed_first = await Runner.run(non_streamed_agent, "hello")
    assert len(non_streamed_first.interruptions) == 1
    non_streamed_state = non_streamed_first.to_state()
    non_streamed_state.approve(non_streamed_first.interruptions[0])
    non_streamed = await Runner.run(non_streamed_agent, non_streamed_state)

    assert len(streamed.tool_input_guardrail_results) == len(
        non_streamed.tool_input_guardrail_results
    )
    assert len(streamed.tool_output_guardrail_results) == len(
        non_streamed.tool_output_guardrail_results
    )


@pytest.mark.asyncio
async def test_streamed_resume_handoff_turn_reports_tool_guardrail_results():
    """A resumed streamed turn that hands off keeps the guardrail results it produced."""

    def _build() -> tuple[FakeModel, Agent[Any]]:
        @tool_input_guardrail
        def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

        @tool_output_guardrail
        def record_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

        @function_tool(
            name_override="approval_tool",
            needs_approval=True,
            tool_input_guardrails=[record_input],
            tool_output_guardrails=[record_output],
        )
        def approval_tool() -> str:
            return "approved-result"

        model = FakeModel()
        target = Agent(name="target", model=model)
        agent = Agent(
            name="TestAgent",
            model=model,
            tools=[approval_tool],
            handoffs=[target],
        )
        model.add_multiple_turn_outputs(
            [
                [
                    get_function_tool_call("approval_tool", "{}", call_id="call_approval"),
                    get_handoff_tool_call(target),
                ],
                [get_text_message("done")],
            ]
        )
        return model, agent

    _, streamed_agent = _build()
    streamed_first = Runner.run_streamed(streamed_agent, input="hello")
    await consume_stream(streamed_first)
    assert len(streamed_first.interruptions) == 1
    streamed = await resume_streamed_after_first_approval(streamed_agent, streamed_first)

    assert streamed.final_output == "done"
    assert len(streamed.tool_input_guardrail_results) == 1
    assert len(streamed.tool_output_guardrail_results) == 1

    _, non_streamed_agent = _build()
    non_streamed_first = await Runner.run(non_streamed_agent, "hello")
    non_streamed_state = non_streamed_first.to_state()
    non_streamed_state.approve(non_streamed_first.interruptions[0])
    non_streamed = await Runner.run(non_streamed_agent, non_streamed_state)

    assert len(streamed.tool_input_guardrail_results) == len(
        non_streamed.tool_input_guardrail_results
    )
    assert len(streamed.tool_output_guardrail_results) == len(
        non_streamed.tool_output_guardrail_results
    )


@pytest.mark.asyncio
async def test_streamed_cancel_during_output_guardrail_starts_no_final_turn_write() -> None:
    """Immediate cancel() must not start a final-turn session write.

    `cancel()` in its default immediate mode cancels outstanding work; `after_turn` is the mode
    that finishes the turn and saves. A cancellation raised inside an in-flight output guardrail
    must therefore not be treated like a guardrail error, or `stream_events()` would stay blocked
    on whatever the session backend does.
    """
    entered_guardrail = asyncio.Event()
    never_set = asyncio.Event()
    cancelled = False
    tool_call_count = 0

    async def parked_output_guardrail(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        entered_guardrail.set()
        await never_set.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    class BlockingAfterCancelSession(SimpleListSession):
        """Writes before the cancel are the turn's own; any write after it would hang the stream."""

        def __init__(self) -> None:
            super().__init__()
            self.wrote_after_cancel = False

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            if cancelled:
                self.wrote_after_cancel = True
                await never_set.wait()
            await super().add_items(items)

    @function_tool(name_override="commit_tool")
    def commit_tool() -> str:
        nonlocal tool_call_count
        tool_call_count += 1
        return "committed-result"

    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("assistant-preamble"),
            get_function_tool_call("commit_tool", "{}", call_id="call-cancel"),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[commit_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=parked_output_guardrail)],
    )
    session = BlockingAfterCancelSession()

    result = Runner.run_streamed(agent, "Use commit_tool", session=session)

    async def drain() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for _event in result.stream_events():
                pass

    drain_task = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(entered_guardrail.wait(), timeout=1)
        cancelled = True
        result.cancel()
        # A final-turn write here would block on never_set and hang the stream.
        await asyncio.wait_for(drain_task, timeout=1)
    finally:
        if not drain_task.done():
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task

    assert session.wrote_after_cancel is False
    assert tool_call_count == 1
    saved_items = await session.get_items()
    saved = [item.get("type") or item.get("role") for item in saved_items if isinstance(item, dict)]
    assert saved == ["user"]
