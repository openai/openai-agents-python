from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai import APIConnectionError
from openai.types.responses import (
    ResponseApplyPatchToolCall,
    ResponseCodeInterpreterToolCall,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseCustomToolCall,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseInProgressEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseOutputTextAnnotationAddedEvent,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseReasoningTextDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseRefusalDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
)
from openai.types.responses.response_output_item import ImageGenerationCall, McpCall
from openai.types.responses.response_output_text import (
    AnnotationFilePath,
    AnnotationURLCitation,
    Logprob,
    LogprobTopLogprob,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam, Variables
from openai.types.responses.response_reasoning_item import Content, ResponseReasoningItem, Summary
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import ValidationError

from agents import (
    Agent,
    ModelBehaviorError,
    ModelRetryAdvice,
    ModelRetryAdviceRequest,
    ModelRetrySettings,
    RunConfig,
    Runner,
    handoff,
    retry_policies,
)
from agents.agent_output import AgentOutputSchema
from agents.items import TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.testing import (
    InvalidModelStep,
    ModelCall,
    ModelStep,
    ModelStepSpec,
    ScriptedModel,
    UnconsumedModelSteps,
    UnexpectedModelCall,
    assistant_message,
    function_call,
)
from agents.tracing import SpanError
from agents.tracing.scope import Scope
from agents.usage import RequestUsage, Usage, _extract_raw_usage_snapshot
from agents.util._error_tracing import REDACTED_TRACE_ERROR_MESSAGE

from .model_test_helpers import get_response_obj
from .test_responses import get_function_tool
from .testing_processor import fetch_span_errors


@pytest.mark.asyncio
async def test_scripted_model_runs_tool_workflow_and_records_calls() -> None:
    model = ScriptedModel(
        [
            [function_call("lookup", {"city": "Tokyo"}, call_id="call_1")],
            [assistant_message("sunny")],
        ]
    )
    agent = Agent(
        name="weather",
        model=model,
        tools=[get_function_tool("lookup", "tool result")],
    )

    result = await Runner.run(agent, "weather?")

    assert result.final_output == "sunny"
    assert len(model.calls) == 2
    assert model.first_call is not None
    assert model.first_call.input == [{"content": "weather?", "role": "user"}]
    assert model.last_call is not None
    assert model.last_call.streamed is False
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_generates_stream_events() -> None:
    model = ScriptedModel([[assistant_message("hello")]])
    result = Runner.run_streamed(Agent(name="test", model=model), "hi")

    events = [event async for event in result.stream_events()]

    assert result.final_output == "hello"
    assert events
    assert model.last_call is not None
    assert model.last_call.streamed is True
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_stream_preserves_request_usage_details() -> None:
    request_entries = [
        RequestUsage(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
            input_tokens_details=InputTokensDetails.model_validate(
                {"cached_tokens": 1, "cache_write_tokens": 0}
            ),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=1),
        ),
        RequestUsage(
            input_tokens=7,
            output_tokens=4,
            total_tokens=11,
            input_tokens_details=InputTokensDetails.model_validate(
                {"cached_tokens": 2, "cache_write_tokens": 3}
            ),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=2),
        ),
    ]
    usage = Usage(
        requests=5,
        input_tokens=10,
        output_tokens=6,
        total_tokens=16,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cached_tokens": 3, "cache_write_tokens": 3}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=3),
        request_usage_entries=request_entries,
    )
    streamed_model = ScriptedModel([ModelStep(output=[assistant_message("hello")], usage=usage)])
    non_streamed_model = ScriptedModel(
        [ModelStep(output=[assistant_message("hello")], usage=usage)]
    )

    streamed_result = Runner.run_streamed(Agent(name="streamed", model=streamed_model), "hi")
    async for _event in streamed_result.stream_events():
        pass
    non_streamed_result = await Runner.run(
        Agent(name="non-streamed", model=non_streamed_model), "hi"
    )

    propagated = streamed_result.context_wrapper.usage
    assert propagated.requests == 5
    assert propagated.input_tokens == 10
    assert propagated.output_tokens == 6
    assert propagated.total_tokens == 16
    assert propagated.request_usage_entries == request_entries
    assert propagated == non_streamed_result.context_wrapper.usage


@pytest.mark.asyncio
async def test_scripted_model_stream_counts_default_usage_as_one_request() -> None:
    streamed_model = ScriptedModel([[assistant_message("streamed")]])
    non_streamed_model = ScriptedModel([[assistant_message("non-streamed")]])

    streamed_result = Runner.run_streamed(Agent(name="streamed", model=streamed_model), "hi")
    async for _event in streamed_result.stream_events():
        pass
    non_streamed_result = await Runner.run(
        Agent(name="non-streamed", model=non_streamed_model), "hi"
    )

    assert streamed_result.context_wrapper.usage.requests == 1
    assert non_streamed_result.context_wrapper.usage.requests == 1


@pytest.mark.asyncio
async def test_scripted_model_preserves_explicit_zero_request_usage_across_run_modes() -> None:
    usage = Usage(
        input_tokens=2,
        output_tokens=1,
        total_tokens=3,
        request_usage_entries=[
            RequestUsage(
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                input_tokens_details=InputTokensDetails.model_validate(
                    {"cached_tokens": 0, "cache_write_tokens": 0}
                ),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
            )
        ],
    )
    streamed_model = ScriptedModel([ModelStep(output=[assistant_message("streamed")], usage=usage)])
    non_streamed_model = ScriptedModel(
        [ModelStep(output=[assistant_message("non-streamed")], usage=usage)]
    )

    streamed_result = Runner.run_streamed(Agent(name="streamed", model=streamed_model), "hi")
    async for _event in streamed_result.stream_events():
        pass
    non_streamed_result = await Runner.run(
        Agent(name="non-streamed", model=non_streamed_model), "hi"
    )

    assert streamed_result.context_wrapper.usage == usage
    assert non_streamed_result.context_wrapper.usage == usage


@pytest.mark.asyncio
async def test_scripted_model_copies_default_usage_before_retry_accounting() -> None:
    default_usage = Usage(requests=1, input_tokens=2, total_tokens=2)
    model = ScriptedModel(
        [
            APIConnectionError(
                message="connection error",
                request=httpx.Request("POST", "https://example.com"),
            ),
            [assistant_message("first")],
            [assistant_message("second")],
        ],
        default_usage=default_usage,
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

    first_result = await Runner.run(agent, "first")
    second_result = await Runner.run(agent, "second")

    assert first_result.context_wrapper.usage.requests == 2
    assert second_result.context_wrapper.usage.requests == 1
    assert default_usage == Usage(requests=1, input_tokens=2, total_tokens=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [None, ""])
async def test_scripted_model_stream_preserves_response_id(response_id: str | None) -> None:
    streamed_model = ScriptedModel(
        [ModelStep(output=[assistant_message("streamed")], response_id=response_id)]
    )
    non_streamed_model = ScriptedModel(
        [ModelStep(output=[assistant_message("non-streamed")], response_id=response_id)]
    )

    streamed_result = Runner.run_streamed(Agent(name="streamed", model=streamed_model), "hi")
    async for _event in streamed_result.stream_events():
        pass
    non_streamed_result = await Runner.run(
        Agent(name="non-streamed", model=non_streamed_model), "hi"
    )

    assert streamed_result.last_response_id == response_id
    assert non_streamed_result.last_response_id == response_id


@pytest.mark.asyncio
async def test_scripted_model_stream_does_not_chain_absent_response_id() -> None:
    def respond(call: ModelCall):
        assert call.previous_response_id is None
        return [assistant_message("done")]

    model = ScriptedModel(
        [
            ModelStep(
                output=[function_call("lookup", {}, call_id="call_1")],
                response_id=None,
            ),
            ModelStep.respond(respond),
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("lookup", "tool result")],
    )

    result = Runner.run_streamed(agent, "hi", auto_previous_response_id=True)
    async for _event in result.stream_events():
        pass

    assert result.final_output == "done"
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_supports_dynamic_responder() -> None:
    def respond(call):
        assert isinstance(call.input, list)
        return [assistant_message(str(call.input[0]["content"]))]

    model = ScriptedModel([ModelStep.respond(respond)])

    result = await Runner.run(Agent(name="test", model=model), "hello")

    assert result.final_output == "hello"


@pytest.mark.asyncio
async def test_scripted_model_accepts_step_mapping() -> None:
    usage = Usage(requests=2, input_tokens=3, output_tokens=4, total_tokens=7)
    model = ScriptedModel(
        [
            {
                "output": [assistant_message("mapped")],
                "usage": usage,
                "response_id": "resp_mapped",
                "request_id": "req_mapped",
                "raw_usage": {"source": "mapping"},
            }
        ]
    )

    response = await model.get_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert response.output == [assistant_message("mapped")]
    assert response.usage == usage
    assert response.response_id == "resp_mapped"
    assert response.request_id == "req_mapped"
    assert response.raw_usage is None
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_snapshots_model_settings_in_recorded_calls() -> None:
    settings = ModelSettings(
        tool_choice="auto",
        extra_args={"provider": {"mode": "first"}},
    )
    model = ScriptedModel([[assistant_message("first")], [assistant_message("second")]])

    async for _event in model.stream_response(
        None,
        [],
        settings,
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        pass
    settings.tool_choice = "none"
    assert settings.extra_args is not None
    settings.extra_args["provider"]["mode"] = "second"
    await model.get_response(
        None,
        [],
        settings,
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    settings.extra_args["provider"]["mode"] = "after"

    first_settings = model.calls[0].model_settings
    second_settings = model.calls[1].model_settings
    assert first_settings is not settings
    assert first_settings.tool_choice == "auto"
    assert first_settings.extra_args == {"provider": {"mode": "first"}}
    assert second_settings.tool_choice == "none"
    assert second_settings.extra_args == {"provider": {"mode": "second"}}


@pytest.mark.asyncio
async def test_scripted_model_snapshots_input_and_prompt_in_recorded_calls() -> None:
    input_item: dict[str, Any] = {
        "role": "user",
        "content": [{"type": "input_text", "text": "first"}],
    }
    input_items = cast(list[TResponseInputItem], [input_item])
    prompt_variables: dict[str, Variables] = {"topic": "first"}
    prompt: ResponsePromptParam = {
        "id": "pmpt_1",
        "variables": prompt_variables,
    }
    model = ScriptedModel([[assistant_message("done")]])

    await model.get_response(
        None,
        input_items,
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=prompt,
    )
    input_item["content"][0]["text"] = "second"
    prompt_variables["topic"] = "second"

    assert model.last_call is not None
    assert model.last_call.input == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "first"}],
        }
    ]
    assert model.last_call.prompt == {
        "id": "pmpt_1",
        "variables": {"topic": "first"},
    }


@pytest.mark.asyncio
async def test_scripted_model_exposes_detached_read_only_call_history() -> None:
    input_item: dict[str, Any] = {
        "role": "user",
        "content": [{"type": "input_text", "text": "first"}],
    }
    settings = ModelSettings(extra_args={"provider": {"mode": "first"}})
    tool = get_function_tool("lookup", "tool result")
    target = Agent(name="delegate")
    handoff_value = handoff(target)
    output_schema = AgentOutputSchema(str)
    model = ScriptedModel([[assistant_message("done")]])

    await model.get_response(
        None,
        cast(list[TResponseInputItem], [input_item]),
        settings,
        [tool],
        output_schema,
        [handoff_value],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    history = model.calls
    assert isinstance(history, tuple)
    with pytest.raises(AttributeError):
        cast(Any, history).append(history[0])

    returned = history[0]
    returned.input[0]["content"][0]["text"] = "changed"
    assert returned.model_settings.extra_args is not None
    returned.model_settings.extra_args["provider"]["mode"] = "changed"
    returned.tools.clear()
    returned.handoffs.clear()

    retained = model.calls[0]
    assert retained.input[0]["content"][0]["text"] == "first"
    assert retained.model_settings.extra_args == {"provider": {"mode": "first"}}
    assert retained.tools == [tool]
    assert retained.handoffs == [handoff_value]
    assert retained.tools[0] is tool
    assert retained.handoffs[0] is handoff_value
    assert retained.output_schema is output_schema
    assert retained.tracing is ModelTracing.DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_scripted_model_snapshot_failure_has_no_recording_side_effects(
    streamed: bool,
) -> None:
    expected = RuntimeError("settings snapshot failed")

    class Uncopyable:
        def __deepcopy__(self, _memo: dict[int, Any]) -> Any:
            raise expected

    responder_calls: list[ModelCall] = []

    def respond(call: ModelCall) -> list[TResponseOutputItem]:
        responder_calls.append(call)
        return [assistant_message("unexpected")]

    model = ScriptedModel([ModelStep.respond(respond)])
    settings = ModelSettings(extra_args={"sentinel": Uncopyable()})

    with pytest.raises(RuntimeError, match="settings snapshot failed") as exc_info:
        if streamed:
            async for _event in model.stream_response(
                None,
                [],
                settings,
                [],
                None,
                [],
                ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            ):
                pass
        else:
            await model.get_response(
                None,
                [],
                settings,
                [],
                None,
                [],
                ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )

    assert exc_info.value is expected
    assert model.calls == ()
    assert model.remaining_steps == 1
    assert responder_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("preserve_raw_usage", [None, False, True])
async def test_scripted_model_gates_and_snapshots_raw_usage(
    streamed: bool,
    preserve_raw_usage: bool | None,
) -> None:
    raw_usage = {"provider": {"input_tokens": 3}}
    model = ScriptedModel([ModelStep(output=[assistant_message("done")], raw_usage=raw_usage)])
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(preserve_raw_usage=preserve_raw_usage),
    )

    if streamed:
        result = Runner.run_streamed(agent, "hi")
        terminal_raw_usage = None
        saw_terminal = False
        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseCompletedEvent
            ):
                saw_terminal = True
                terminal_raw_usage = _extract_raw_usage_snapshot(event.data.response)
        assert saw_terminal is True
    else:
        result = await Runner.run(agent, "hi")
        terminal_raw_usage = None

    raw_usage["provider"]["input_tokens"] = 99
    expected_raw_usage = {"provider": {"input_tokens": 3}} if preserve_raw_usage is True else None
    assert result.raw_responses[0].raw_usage == expected_raw_usage
    if streamed:
        assert terminal_raw_usage == expected_raw_usage


@pytest.mark.asyncio
async def test_scripted_model_withholds_raw_usage_until_stream_completion() -> None:
    raw_usage = {"provider": {"input_tokens": 3}}
    model = ScriptedModel([ModelStep(output=[assistant_message("done")], raw_usage=raw_usage)])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(preserve_raw_usage=True),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    response_events = [
        event
        for event in events
        if isinstance(
            event,
            ResponseCreatedEvent | ResponseInProgressEvent | ResponseCompletedEvent,
        )
    ]
    assert [_extract_raw_usage_snapshot(event.response) for event in response_events] == [
        None,
        None,
        raw_usage,
    ]


@pytest.mark.asyncio
async def test_scripted_model_accepts_step_mappings_in_queue_methods() -> None:
    model = ScriptedModel()
    model.enqueue({"output": [assistant_message("first")]})
    model.extend([{"output": [assistant_message("second")]}])
    agent = Agent(name="test", model=model)

    first = await Runner.run(agent, "first")
    second = await Runner.run(agent, "second")

    assert first.final_output == "first"
    assert second.final_output == "second"
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_responder_can_return_step_mapping() -> None:
    def respond(call: ModelCall) -> ModelStepSpec:
        assert isinstance(call.input, list)
        return {"output": [assistant_message(str(call.input[0]["content"]))]}

    model = ScriptedModel([ModelStep.respond(respond)])

    result = await Runner.run(Agent(name="test", model=model), "mapped")

    assert result.final_output == "mapped"
    model.assert_complete()


def test_scripted_model_rejects_unknown_step_mapping_keys() -> None:
    invalid_step = cast(ModelStepSpec, {"unknown": True})

    with pytest.raises(InvalidModelStep, match=r"step #1") as exc_info:
        ScriptedModel([invalid_step])

    assert exc_info.value.reason == "unsupported_field"
    assert exc_info.value.input_index == 0


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        ({"error": "not an exception"}, "invalid_error"),
        ({"responder": "not callable"}, "invalid_responder"),
        ({"stream_events": 1}, "invalid_stream_events"),
        (
            {"error": RuntimeError("failed"), "responder": lambda _call: []},
            "conflicting_outcomes",
        ),
        ({"output": "not an output sequence"}, "invalid_input"),
        ({"usage": {"requests": 1}}, "invalid_input"),
        ({"response_id": 1}, "invalid_input"),
        ({"raw_usage": []}, "invalid_input"),
        ({"retry_advice": ModelRetryAdvice(suggested=True)}, "invalid_retry_advice"),
        (
            {"error": RuntimeError("failed"), "retry_advice": "not advice"},
            "invalid_retry_advice",
        ),
    ],
)
def test_scripted_model_validates_step_envelopes_before_queuing(
    step: object,
    reason: str,
) -> None:
    model = ScriptedModel([[assistant_message("retained")]])

    with pytest.raises(InvalidModelStep, match=r"step #1") as exc_info:
        model.extend(cast(Any, [step]))

    assert exc_info.value.reason == reason
    assert exc_info.value.input_index == 0
    assert model.remaining_steps == 1


def test_scripted_model_reports_the_zero_origin_index_of_an_invalid_step() -> None:
    with pytest.raises(InvalidModelStep, match=r"step #2") as exc_info:
        ScriptedModel([[assistant_message("valid")], cast(Any, {"unknown": True})])

    assert exc_info.value.reason == "unsupported_field"
    assert exc_info.value.input_index == 1


@pytest.mark.asyncio
async def test_scripted_model_supports_awaitable_recursive_responder() -> None:
    async def outer_responder(call: ModelCall) -> ModelStep:
        def inner_responder(inner_call: ModelCall) -> list[TResponseOutputItem]:
            assert inner_call is call
            return [assistant_message("nested")]

        return ModelStep.respond(inner_responder)

    model = ScriptedModel([ModelStep.respond(outer_responder)])

    result = await Runner.run(Agent(name="test", model=model), "hello")

    assert result.final_output == "nested"
    assert len(model.calls) == 1
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_model_retry_advice_is_error_scoped_and_detached() -> None:
    error = RuntimeError("failed")
    advice = ModelRetryAdvice(suggested=True, replay_safety="safe")
    model = ScriptedModel([ModelStep.raise_error(error, retry_advice=advice)])
    advice.suggested = False

    with pytest.raises(RuntimeError, match="failed"):
        await model.get_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    request = ModelRetryAdviceRequest(error=error, attempt=1, stream=False)
    first = model.get_retry_advice(request)
    assert first is not None
    assert first.suggested is True
    first.suggested = False
    second = model.get_retry_advice(request)
    assert second is not None
    assert second.suggested is True
    other_request = ModelRetryAdviceRequest(
        error=RuntimeError("other"),
        attempt=1,
        stream=False,
    )
    assert model.get_retry_advice(other_request) is None


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_scripted_model_clears_stale_retry_advice_after_responder_failure(
    streamed: bool,
) -> None:
    error = RuntimeError("failed")

    def raise_same_error(_call: ModelCall) -> ModelStep:
        raise error

    model = ScriptedModel(
        [
            ModelStep.raise_error(error, retry_advice=ModelRetryAdvice(suggested=True)),
            ModelStep.respond(raise_same_error),
            [assistant_message("must remain unconsumed")],
        ]
    )

    async def invoke() -> None:
        if streamed:
            async for _event in model.stream_response(
                None,
                [],
                ModelSettings(),
                [],
                None,
                [],
                ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            ):
                pass
        else:
            await model.get_response(
                None,
                [],
                ModelSettings(),
                [],
                None,
                [],
                ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )

    request = ModelRetryAdviceRequest(error=error, attempt=1, stream=streamed)
    with pytest.raises(RuntimeError, match="failed"):
        await invoke()
    assert model.get_retry_advice(request) is not None

    with pytest.raises(RuntimeError, match="failed"):
        await invoke()
    assert model.get_retry_advice(request) is None
    assert model.remaining_steps == 1


@pytest.mark.asyncio
async def test_scripted_model_supports_exact_normalized_stream() -> None:
    output = [assistant_message("exact")]

    async def stream(_call) -> AsyncIterator[TResponseStreamEvent]:
        yield ResponseCompletedEvent(
            type="response.completed",
            response=get_response_obj(output),
            sequence_number=0,
        )

    model = ScriptedModel([ModelStep.stream(stream, output=output)])
    result = Runner.run_streamed(Agent(name="test", model=model), "hi")

    async for _event in result.stream_events():
        pass

    assert result.final_output == "exact"


def test_model_step_freezes_exact_stream_event_sequence() -> None:
    event = ResponseCompletedEvent(
        type="response.completed",
        response=get_response_obj([]),
        sequence_number=0,
    )
    events = [event]

    step = ModelStep.stream(events)
    events.clear()

    assert step.stream_events == (event,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_item", "item_kind"),
    [
        (
            ResponseCustomToolCall(
                call_id="call_1",
                id="item_1",
                input="payload",
                name="custom",
                type="custom_tool_call",
            ),
            "ResponseCustomToolCall",
        ),
        (
            ResponseCodeInterpreterToolCall(
                id="item_1",
                code="print('hello')",
                container_id="container_1",
                outputs=None,
                status="completed",
                type="code_interpreter_call",
            ),
            "ResponseCodeInterpreterToolCall",
        ),
        (
            McpCall(
                id="item_1",
                arguments='{"query":"hello"}',
                name="search",
                server_label="docs",
                status="completed",
                type="mcp_call",
            ),
            "McpCall",
        ),
        (
            ImageGenerationCall(
                id="item_1",
                result="base64-image",
                status="completed",
                type="image_generation_call",
            ),
            "ImageGenerationCall",
        ),
    ],
)
async def test_scripted_model_rejects_unsupported_automatic_tool_streams(
    output_item: TResponseOutputItem,
    item_kind: str,
) -> None:
    model = ScriptedModel([[output_item]])
    yielded: list[TResponseStreamEvent] = []

    with pytest.raises(
        ModelBehaviorError,
        match=rf"Automatic streaming does not support {item_kind}.*ModelStep\.stream",
    ):
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            yielded.append(event)

    assert yielded == []


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_method", ["constructor", "enqueue", "extend"])
async def test_scripted_model_snapshots_static_steps_when_queued(queue_method: str) -> None:
    output_item = assistant_message("before")
    usage = Usage(requests=2, input_tokens=3, output_tokens=4, total_tokens=7)
    raw_usage = {"provider": {"cached_tokens": 1}}
    step = ModelStep(
        output=[output_item],
        usage=usage,
        response_id="resp_before",
        raw_usage=raw_usage,
    )

    if queue_method == "constructor":
        model = ScriptedModel([step])
    else:
        model = ScriptedModel()
        if queue_method == "enqueue":
            model.enqueue(step)
        else:
            model.extend([step])

    message = cast(ResponseOutputMessage, output_item)
    text = cast(ResponseOutputText, message.content[0])
    text.text = "after"
    usage.requests = 99
    raw_usage["provider"]["cached_tokens"] = 99
    step.response_id = "resp_after"

    response = await model.get_response(
        None,
        [],
        ModelSettings(preserve_raw_usage=True),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    queued_message = cast(ResponseOutputMessage, response.output[0])
    queued_text = cast(ResponseOutputText, queued_message.content[0])
    assert queued_text.text == "before"
    assert response.usage == Usage(requests=2, input_tokens=3, output_tokens=4, total_tokens=7)
    assert response.response_id == "resp_before"
    assert response.raw_usage == {"provider": {"cached_tokens": 1}}


@pytest.mark.asyncio
async def test_scripted_model_snapshots_output_shorthand_when_queued() -> None:
    output_item = assistant_message("before")
    output = [output_item]
    model = ScriptedModel([output])

    message = cast(ResponseOutputMessage, output_item)
    text = cast(ResponseOutputText, message.content[0])
    text.text = "after"
    output.clear()

    response = await model.get_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    queued_message = cast(ResponseOutputMessage, response.output[0])
    queued_text = cast(ResponseOutputText, queued_message.content[0])
    assert queued_text.text == "before"


@pytest.mark.asyncio
async def test_scripted_model_records_stream_factory_errors_on_generation_span() -> None:
    async def stream(_call) -> AsyncIterator[TResponseStreamEvent]:
        yield ResponseCreatedEvent(
            type="response.created",
            response=get_response_obj([]),
            sequence_number=0,
        )
        raise RuntimeError("stream failed")

    model = ScriptedModel([ModelStep.stream(stream)], emit_traces=True)

    with pytest.raises(RuntimeError, match="stream failed"):
        result = Runner.run_streamed(Agent(name="test", model=model), "hi")
        async for _event in result.stream_events():
            pass

    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {"name": "RuntimeError", "message": "stream failed"},
        }
    ]


@pytest.mark.asyncio
async def test_scripted_model_marks_span_current_only_while_advancing_stream_factory() -> None:
    observed_spans: list[Any] = []

    async def stream(_call) -> AsyncIterator[TResponseStreamEvent]:
        observed_spans.append(Scope.get_current_span())
        yield ResponseCreatedEvent(
            type="response.created",
            response=get_response_obj([]),
            sequence_number=0,
        )
        observed_spans.append(Scope.get_current_span())

    model = ScriptedModel([ModelStep.stream(stream)], emit_traces=True)

    async for _event in model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        assert Scope.get_current_span() is None

    assert len(observed_spans) == 2
    assert observed_spans[0] is observed_spans[1]
    assert observed_spans[0] is not None
    assert observed_spans[0].span_data.type == "generation"


@pytest.mark.asyncio
async def test_scripted_model_closes_exact_stream_when_outer_stream_is_closed() -> None:
    closed = False
    close_span: Any = None

    async def stream(_call) -> AsyncIterator[TResponseStreamEvent]:
        nonlocal closed, close_span
        try:
            yield ResponseCreatedEvent(
                type="response.created",
                response=get_response_obj([]),
                sequence_number=0,
            )
            await asyncio.Event().wait()
        finally:
            closed = True
            close_span = Scope.get_current_span()

    model = ScriptedModel([ModelStep.stream(stream)], emit_traces=True)
    outer = model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    await anext(outer)
    assert closed is False
    await outer.aclose()

    assert closed is True
    assert close_span is not None
    assert close_span.span_data.type == "generation"
    assert Scope.get_current_span() is None


@pytest.mark.asyncio
async def test_scripted_model_closes_exact_stream_when_consumer_is_cancelled() -> None:
    blocked = asyncio.Event()
    closed = asyncio.Event()

    async def stream(_call) -> AsyncIterator[TResponseStreamEvent]:
        try:
            yield ResponseCreatedEvent(
                type="response.created",
                response=get_response_obj([]),
                sequence_number=0,
            )
            blocked.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    model = ScriptedModel([ModelStep.stream(stream)])
    outer = model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    await anext(outer)
    pending = asyncio.create_task(anext(outer))
    await blocked.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed.is_set()


@pytest.mark.asyncio
async def test_scripted_model_preserves_cancellation_when_exact_stream_close_fails() -> None:
    class FailingCloseStream:
        def __init__(self) -> None:
            self.blocked = asyncio.Event()
            self.close_calls = 0

        def __aiter__(self) -> FailingCloseStream:
            return self

        async def __anext__(self) -> TResponseStreamEvent:
            self.blocked.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("close failed")

    inner = FailingCloseStream()
    model = ScriptedModel([ModelStep.stream(lambda _call: inner)])
    outer = model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )
    pending = asyncio.create_task(anext(outer))
    await inner.blocked.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert inner.close_calls == 1


@pytest.mark.asyncio
async def test_scripted_model_surfaces_exact_stream_close_failure_after_exhaustion() -> None:
    close_error = RuntimeError("close failed")

    class FailingCloseStream:
        def __aiter__(self) -> FailingCloseStream:
            return self

        async def __anext__(self) -> TResponseStreamEvent:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            raise close_error

    inner = FailingCloseStream()
    model = ScriptedModel([ModelStep.stream(lambda _call: inner)], emit_traces=True)
    agent = Agent(name="test", model=model)

    with pytest.raises(RuntimeError, match="close failed") as exc_info:
        result = Runner.run_streamed(agent, "hi")
        async for _event in result.stream_events():
            pass

    assert exc_info.value is close_error
    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {"name": "RuntimeError", "message": "close failed"},
        }
    ]


@pytest.mark.asyncio
async def test_scripted_model_preserves_exact_stream_error_when_close_fails() -> None:
    stream_error = RuntimeError("stream failed")

    class FailingStream:
        def __init__(self) -> None:
            self.close_calls = 0

        def __aiter__(self) -> FailingStream:
            return self

        async def __anext__(self) -> TResponseStreamEvent:
            raise stream_error

        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("close failed")

    inner = FailingStream()
    model = ScriptedModel([ModelStep.stream(lambda _call: inner)], emit_traces=True)
    agent = Agent(name="test", model=model)

    with pytest.raises(RuntimeError, match="stream failed") as exc_info:
        result = Runner.run_streamed(agent, "hi")
        async for _event in result.stream_events():
            pass

    assert exc_info.value is stream_error
    assert inner.close_calls == 1
    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {"name": "RuntimeError", "message": "stream failed"},
        }
    ]


@pytest.mark.parametrize("emit_traces", [False, True])
def test_scripted_model_early_stream_exit_has_task_safe_span_cleanup(emit_traces: bool) -> None:
    loop = asyncio.new_event_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def consume_one_event() -> None:
        model = ScriptedModel([[assistant_message("hello")]], emit_traces=emit_traces)
        async for _event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            assert Scope.get_current_span() is None
            break

    try:
        loop.run_until_complete(consume_one_event())
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()

    assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_scripted_model_preserves_unformattable_responder_error(streamed: bool) -> None:
    class UnformattableError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("format failed")

    expected = UnformattableError()

    def respond(_call: ModelCall) -> Any:
        raise expected

    model = ScriptedModel([ModelStep.respond(respond)], emit_traces=True)
    agent = Agent(name="test", model=model)

    with pytest.raises(UnformattableError) as exc_info:
        if streamed:
            result = Runner.run_streamed(agent, "hi")
            async for _event in result.stream_events():
                pass
        else:
            await Runner.run(agent, "hi")

    assert exc_info.value is expected
    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {
                "name": "UnformattableError",
                "message": "Unrenderable UnformattableError",
            },
        }
    ]


def test_scripted_model_ignores_span_attachment_failure() -> None:
    class FailingSpan:
        def set_error(self, _error: SpanError) -> None:
            raise KeyboardInterrupt

    ScriptedModel._set_span_error(FailingSpan(), RuntimeError("model failed"), ModelTracing.ENABLED)


def test_scripted_model_contains_base_exception_from_error_formatting() -> None:
    class UnformattableError(RuntimeError):
        def __str__(self) -> str:
            raise KeyboardInterrupt

    class RecordingSpan:
        def __init__(self) -> None:
            self.error: SpanError | None = None

        def set_error(self, error: SpanError) -> None:
            self.error = error

    span = RecordingSpan()
    ScriptedModel._set_span_error(span, UnformattableError(), ModelTracing.ENABLED)

    assert span.error == SpanError(
        message="Error",
        data={
            "name": "UnformattableError",
            "message": "Unrenderable UnformattableError",
        },
    )


def test_scripted_model_redacts_span_error_without_rendering_exception() -> None:
    class SensitiveError(RuntimeError):
        def __init__(self) -> None:
            self.str_calls = 0

        def __str__(self) -> str:
            self.str_calls += 1
            return "sensitive payload"

    class RecordingSpan:
        def __init__(self) -> None:
            self.error: SpanError | None = None

        def set_error(self, error: SpanError) -> None:
            self.error = error

    error = SensitiveError()
    span = RecordingSpan()

    ScriptedModel._set_span_error(span, error, ModelTracing.ENABLED_WITHOUT_DATA)

    assert error.str_calls == 0
    assert span.error == SpanError(
        message="Error",
        data={
            "name": "SensitiveError",
            "message": REDACTED_TRACE_ERROR_MESSAGE,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("awaitable", [False, True])
async def test_scripted_model_records_responder_errors_on_generation_span(
    streamed: bool,
    awaitable: bool,
) -> None:
    expected = RuntimeError("responder failed")

    def respond(_call: ModelCall) -> Any:
        raise expected

    async def respond_async(_call: ModelCall) -> Any:
        raise expected

    model = ScriptedModel(
        [ModelStep.respond(respond_async if awaitable else respond)],
        emit_traces=True,
    )
    agent = Agent(name="test", model=model)

    with pytest.raises(RuntimeError, match="responder failed") as exc_info:
        if streamed:
            result = Runner.run_streamed(agent, "hi")
            async for _event in result.stream_events():
                pass
        else:
            await Runner.run(agent, "hi")

    assert exc_info.value is expected
    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {"name": "RuntimeError", "message": "responder failed"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_scripted_model_redacts_generation_span_errors_from_run_config(
    streamed: bool,
) -> None:
    expected = RuntimeError("sensitive provider payload")
    model = ScriptedModel([ModelStep.raise_error(expected)], emit_traces=True)
    agent = Agent(name="test", model=model)
    run_config = RunConfig(trace_include_sensitive_data=False)

    with pytest.raises(RuntimeError) as exc_info:
        if streamed:
            result = Runner.run_streamed(agent, "hi", run_config=run_config)
            async for _event in result.stream_events():
                pass
        else:
            await Runner.run(agent, "hi", run_config=run_config)

    assert exc_info.value is expected
    assert fetch_span_errors("generation") == [
        {
            "message": "Error",
            "data": {
                "name": "RuntimeError",
                "message": REDACTED_TRACE_ERROR_MESSAGE,
            },
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("call_id", [None, "", 123])
async def test_scripted_model_rejects_invalid_apply_patch_call_id(call_id: Any) -> None:
    output = cast(
        TResponseOutputItem,
        {
            "type": "apply_patch_call",
            "id": "patch_item",
            "call_id": call_id,
            "operation": {"type": "delete_file", "path": "example.txt"},
        },
    )
    model = ScriptedModel([[output]])

    with pytest.raises(
        ModelBehaviorError,
        match="Tool invocations require a non-empty string call ID before execution",
    ):
        await Runner.run(Agent(name="test", model=model), "hi")


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", [None, "", 123])
async def test_scripted_model_rejects_invalid_apply_patch_item_id(item_id: Any) -> None:
    output = cast(
        TResponseOutputItem,
        {
            "type": "apply_patch_call",
            "id": item_id,
            "call_id": "call_1",
            "operation": {"type": "delete_file", "path": "example.txt"},
        },
    )
    model = ScriptedModel([[output]])

    with pytest.raises(
        ModelBehaviorError,
        match="Apply-patch tool calls require a non-empty string item ID when provided",
    ):
        await Runner.run(Agent(name="test", model=model), "hi")


@pytest.mark.asyncio
async def test_scripted_model_defaults_omitted_apply_patch_item_id_to_call_id() -> None:
    output = cast(
        TResponseOutputItem,
        {
            "type": "apply_patch_call",
            "call_id": "call_1",
            "operation": {"type": "delete_file", "path": "example.txt"},
        },
    )
    model = ScriptedModel([[output]])

    response = await model.get_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    item = response.output[0]
    assert isinstance(item, ResponseApplyPatchToolCall)
    assert item.id == "call_1"
    assert item.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "", "invalid"])
async def test_scripted_model_rejects_invalid_explicit_apply_patch_status(status: Any) -> None:
    output = cast(
        TResponseOutputItem,
        {
            "type": "apply_patch_call",
            "id": "patch_item",
            "call_id": "call_1",
            "status": status,
            "operation": {"type": "delete_file", "path": "example.txt"},
        },
    )
    model = ScriptedModel([[output]])

    with pytest.raises(ValidationError):
        await model.get_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )


@pytest.mark.asyncio
async def test_scripted_model_function_argument_events_use_output_item_id() -> None:
    model = ScriptedModel([[function_call("lookup", "{}", call_id="call_1", item_id="item_1")]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    argument_events = [
        event
        for event in events
        if isinstance(
            event,
            ResponseFunctionCallArgumentsDeltaEvent | ResponseFunctionCallArgumentsDoneEvent,
        )
    ]
    assert [event.item_id for event in argument_events] == ["item_1", "item_1"]


@pytest.mark.asyncio
async def test_scripted_model_function_argument_events_preserve_empty_output_item_id() -> None:
    output = function_call("lookup", "{}", call_id="call_1", item_id="")
    model = ScriptedModel([[output]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    item_events = [
        event
        for event in events
        if isinstance(event, ResponseOutputItemAddedEvent | ResponseOutputItemDoneEvent)
    ]
    argument_events = [
        event
        for event in events
        if isinstance(
            event,
            ResponseFunctionCallArgumentsDeltaEvent | ResponseFunctionCallArgumentsDoneEvent,
        )
    ]
    assert [event.item.id for event in item_events] == ["", ""]
    assert [event.item_id for event in argument_events] == ["", ""]


@pytest.mark.asyncio
async def test_scripted_model_function_argument_events_fall_back_to_call_id() -> None:
    output = ResponseFunctionToolCall(
        type="function_call",
        name="lookup",
        arguments="{}",
        call_id="call_1",
    )
    model = ScriptedModel([[output]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    argument_events = [
        event
        for event in events
        if isinstance(
            event,
            ResponseFunctionCallArgumentsDeltaEvent | ResponseFunctionCallArgumentsDoneEvent,
        )
    ]
    assert [event.item_id for event in argument_events] == ["call_1", "call_1"]


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_uses_in_progress_added_payloads() -> None:
    reasoning = ResponseReasoningItem(
        id="reasoning_1",
        summary=[Summary(text="summary", type="summary_text")],
        encrypted_content="encrypted-reasoning",
        type="reasoning",
        status="completed",
    )
    message = assistant_message("hello", item_id="message_1")
    function = function_call(
        "lookup",
        {"city": "Tokyo"},
        call_id="call_1",
        item_id="function_1",
    )
    completed_items = [reasoning, message, function]
    usage = Usage(requests=3, input_tokens=4, output_tokens=5, total_tokens=9)
    model = ScriptedModel([ModelStep(output=completed_items, usage=usage)])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    opening_responses = [
        event.response
        for event in events
        if isinstance(event, ResponseCreatedEvent | ResponseInProgressEvent)
    ]
    assert [response.output for response in opening_responses] == [[], []]
    assert [response.status for response in opening_responses] == ["in_progress", "in_progress"]
    assert [response.usage for response in opening_responses] == [None, None]

    added_items = [
        event.item for event in events if isinstance(event, ResponseOutputItemAddedEvent)
    ]
    assert isinstance(added_items[0], ResponseReasoningItem)
    assert added_items[0].encrypted_content is None
    assert added_items[0].summary == []
    assert added_items[0].status == "in_progress"
    assert isinstance(added_items[1], ResponseOutputMessage)
    assert added_items[1].content == []
    assert added_items[1].status == "in_progress"
    assert isinstance(added_items[2], ResponseFunctionToolCall)
    assert added_items[2].arguments == ""
    assert added_items[2].status == "in_progress"

    added_summary_parts = [
        event.part for event in events if isinstance(event, ResponseReasoningSummaryPartAddedEvent)
    ]
    assert [part.text for part in added_summary_parts] == [""]
    added_content_parts = [
        event.part for event in events if isinstance(event, ResponseContentPartAddedEvent)
    ]
    assert [part.text for part in added_content_parts] == [""]

    done_items = [event.item for event in events if isinstance(event, ResponseOutputItemDoneEvent)]
    assert done_items == completed_items
    assert cast(ResponseReasoningItem, done_items[0]).encrypted_content == "encrypted-reasoning"
    completed_event = next(event for event in events if isinstance(event, ResponseCompletedEvent))
    assert completed_event.response.status == "completed"
    assert completed_event.response.output == completed_items
    assert (
        cast(ResponseReasoningItem, completed_event.response.output[0]).encrypted_content
        == "encrypted-reasoning"
    )
    assert completed_event.response.usage is not None
    assert completed_event.response.usage.input_tokens == 4
    assert completed_event.response.usage.output_tokens == 5
    assert completed_event.response.usage.total_tokens == 9
    request_count_attribute = "_agents_sdk_request_count"
    assert getattr(completed_event.response.usage, request_count_attribute) == 3


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_detaches_done_item_from_terminal_response() -> None:
    model = ScriptedModel([[assistant_message("original", item_id="message_1")]])
    completed_event: ResponseCompletedEvent | None = None

    async for event in model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        if isinstance(event, ResponseOutputItemDoneEvent):
            assert isinstance(event.item, ResponseOutputMessage)
            assert isinstance(event.item.content[0], ResponseOutputText)
            event.item.content[0].text = "mutated"
        elif isinstance(event, ResponseCompletedEvent):
            completed_event = event

    assert completed_event is not None
    terminal_item = completed_event.response.output[0]
    assert isinstance(terminal_item, ResponseOutputMessage)
    assert isinstance(terminal_item.content[0], ResponseOutputText)
    assert terminal_item.content[0].text == "original"


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_detaches_content_part_event_payloads() -> None:
    text = ResponseOutputText.model_validate(
        {
            "type": "output_text",
            "text": "original text",
            "annotations": [],
            "logprobs": [],
            "provider_data": {"nested": ["original text"]},
        }
    )
    refusal = ResponseOutputRefusal.model_validate(
        {
            "type": "refusal",
            "refusal": "original refusal",
            "provider_data": {"nested": ["original refusal"]},
        }
    )
    message = ResponseOutputMessage(
        id="message_1",
        type="message",
        role="assistant",
        status="completed",
        content=[text, refusal],
    )
    model = ScriptedModel([[message]])
    done_parts: list[ResponseOutputText | ResponseOutputRefusal] = []
    done_item: ResponseOutputMessage | None = None
    completed_item: ResponseOutputMessage | None = None

    async for event in model.stream_response(
        None,
        [],
        ModelSettings(),
        [],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        if isinstance(event, ResponseContentPartAddedEvent):
            cast(Any, event.part).provider_data["nested"][0] = "mutated"
        elif isinstance(event, ResponseContentPartDoneEvent):
            done_parts.append(event.part)
        elif isinstance(event, ResponseOutputItemDoneEvent):
            assert isinstance(event.item, ResponseOutputMessage)
            done_item = event.item
        elif isinstance(event, ResponseCompletedEvent):
            assert isinstance(event.response.output[0], ResponseOutputMessage)
            completed_item = event.response.output[0]

    expected_provider_data = [
        {"nested": ["original text"]},
        {"nested": ["original refusal"]},
    ]
    assert [cast(Any, part).provider_data for part in done_parts] == expected_provider_data
    assert done_item is not None
    assert [cast(Any, part).provider_data for part in done_item.content] == expected_provider_data
    assert completed_item is not None
    assert [
        cast(Any, part).provider_data for part in completed_item.content
    ] == expected_provider_data


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_preserves_text_logprobs() -> None:
    output_logprobs = [
        Logprob(
            token="hello",
            bytes=[104, 101, 108, 108, 111],
            logprob=-0.25,
            top_logprobs=[
                LogprobTopLogprob(
                    token="hi",
                    bytes=[104, 105],
                    logprob=-1.5,
                )
            ],
        ),
        Logprob(
            token="!",
            bytes=[33],
            logprob=-0.1,
            top_logprobs=[],
        ),
    ]
    text = ResponseOutputText(
        type="output_text",
        text="hello",
        annotations=[],
        logprobs=output_logprobs,
    )
    message = ResponseOutputMessage(
        id="message_1",
        type="message",
        role="assistant",
        status="completed",
        content=[text],
    )
    model = ScriptedModel([[message]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    added_event = next(
        event for event in events if isinstance(event, ResponseContentPartAddedEvent)
    )
    delta_event = next(event for event in events if isinstance(event, ResponseTextDeltaEvent))
    done_event = next(event for event in events if isinstance(event, ResponseTextDoneEvent))
    assert isinstance(added_event.part, ResponseOutputText)
    assert added_event.part.logprobs == []
    assert [(logprob.token, logprob.logprob) for logprob in delta_event.logprobs] == [
        ("hello", -0.25),
        ("!", -0.1),
    ]
    assert delta_event.logprobs[0].top_logprobs is not None
    assert [logprob.token for logprob in delta_event.logprobs[0].top_logprobs] == ["hi"]
    assert delta_event.logprobs[1].top_logprobs == []
    assert [(logprob.token, logprob.logprob) for logprob in done_event.logprobs] == [
        ("hello", -0.25),
        ("!", -0.1),
    ]
    assert done_event.logprobs[0].top_logprobs is not None
    assert [logprob.token for logprob in done_event.logprobs[0].top_logprobs] == ["hi"]
    assert done_event.logprobs[1].top_logprobs == []

    completed_event = next(event for event in events if isinstance(event, ResponseCompletedEvent))
    completed_message = completed_event.response.output[0]
    assert isinstance(completed_message, ResponseOutputMessage)
    completed_text = completed_message.content[0]
    assert isinstance(completed_text, ResponseOutputText)
    assert completed_text.logprobs == output_logprobs


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_emits_text_annotation_events() -> None:
    annotations = [
        AnnotationURLCitation(
            end_index=5,
            start_index=0,
            title="Example",
            type="url_citation",
            url="https://example.test",
        ),
        AnnotationFilePath(
            file_id="file_1",
            index=6,
            type="file_path",
        ),
    ]
    text = ResponseOutputText(
        type="output_text",
        text="hello file",
        annotations=annotations,
        logprobs=[],
    )
    message = ResponseOutputMessage(
        id="message_1",
        type="message",
        role="assistant",
        status="completed",
        content=[text],
    )
    model = ScriptedModel([[message]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    added_part = next(event for event in events if isinstance(event, ResponseContentPartAddedEvent))
    annotation_events = [
        event for event in events if isinstance(event, ResponseOutputTextAnnotationAddedEvent)
    ]
    text_done = next(event for event in events if isinstance(event, ResponseTextDoneEvent))
    assert isinstance(added_part.part, ResponseOutputText)
    assert added_part.part.annotations == []
    assert [event.annotation_index for event in annotation_events] == [0, 1]
    assert [event.model_dump()["annotation"] for event in annotation_events] == [
        annotation.model_dump() for annotation in annotations
    ]
    assert all(event.item_id == "message_1" for event in annotation_events)
    assert all(event.output_index == 0 for event in annotation_events)
    assert all(event.content_index == 0 for event in annotation_events)
    assert annotation_events[0].sequence_number == added_part.sequence_number + 2
    assert annotation_events[1].sequence_number == annotation_events[0].sequence_number + 1
    assert text_done.sequence_number == annotation_events[1].sequence_number + 1


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_emits_reasoning_content_events() -> None:
    reasoning = ResponseReasoningItem(
        id="reasoning_1",
        summary=[],
        content=[Content(text="think carefully", type="reasoning_text")],
        type="reasoning",
        status="completed",
    )
    model = ScriptedModel([[reasoning]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    added_event = next(event for event in events if isinstance(event, ResponseOutputItemAddedEvent))
    assert isinstance(added_event.item, ResponseReasoningItem)
    assert added_event.item.content == []
    delta_event = next(
        event for event in events if isinstance(event, ResponseReasoningTextDeltaEvent)
    )
    done_event = next(
        event for event in events if isinstance(event, ResponseReasoningTextDoneEvent)
    )
    assert delta_event.item_id == "reasoning_1"
    assert delta_event.output_index == 0
    assert delta_event.content_index == 0
    assert delta_event.delta == "think carefully"
    assert done_event.item_id == "reasoning_1"
    assert done_event.output_index == 0
    assert done_event.content_index == 0
    assert done_event.text == "think carefully"
    assert done_event.sequence_number == delta_event.sequence_number + 1


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_marks_added_apply_patch_call_in_progress() -> None:
    apply_patch_call = ResponseApplyPatchToolCall(
        type="apply_patch_call",
        id="apply_patch_1",
        call_id="call_1",
        status="completed",
        operation=cast(
            Any,
            {"type": "update_file", "path": "test.md", "diff": "-old\n+new\n"},
        ),
    )
    model = ScriptedModel([[apply_patch_call]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    added_event = next(event for event in events if isinstance(event, ResponseOutputItemAddedEvent))
    done_event = next(event for event in events if isinstance(event, ResponseOutputItemDoneEvent))
    completed_event = next(event for event in events if isinstance(event, ResponseCompletedEvent))

    assert isinstance(added_event.item, ResponseApplyPatchToolCall)
    assert added_event.item.status == "in_progress"
    assert isinstance(done_event.item, ResponseApplyPatchToolCall)
    assert done_event.item.status == "completed"
    assert isinstance(completed_event.response.output[0], ResponseApplyPatchToolCall)
    assert completed_event.response.output[0].status == "completed"


@pytest.mark.asyncio
async def test_scripted_model_automatic_stream_emits_refusal_content_events() -> None:
    refusal = ResponseOutputRefusal(type="refusal", refusal="I cannot help with that.")
    message = ResponseOutputMessage(
        id="message_1",
        type="message",
        role="assistant",
        status="completed",
        content=[refusal],
    )
    model = ScriptedModel([[message]])

    events = [
        event
        async for event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    assert [event.type for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.refusal.delta",
        "response.refusal.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event.sequence_number for event in events] == list(range(len(events)))
    content_events = [
        event
        for event in events
        if isinstance(
            event,
            ResponseContentPartAddedEvent
            | ResponseRefusalDeltaEvent
            | ResponseRefusalDoneEvent
            | ResponseContentPartDoneEvent,
        )
    ]
    assert [event.type for event in content_events] == [
        "response.content_part.added",
        "response.refusal.delta",
        "response.refusal.done",
        "response.content_part.done",
    ]
    assert all(event.item_id == "message_1" for event in content_events)
    assert all(event.output_index == 0 for event in content_events)
    assert all(event.content_index == 0 for event in content_events)
    added, delta, refusal_done, content_done = content_events
    assert isinstance(added, ResponseContentPartAddedEvent)
    assert isinstance(added.part, ResponseOutputRefusal)
    assert added.part.refusal == ""
    assert isinstance(delta, ResponseRefusalDeltaEvent)
    assert delta.delta == "I cannot help with that."
    assert isinstance(refusal_done, ResponseRefusalDoneEvent)
    assert refusal_done.refusal == "I cannot help with that."
    assert isinstance(content_done, ResponseContentPartDoneEvent)
    assert content_done.part == refusal


@pytest.mark.asyncio
async def test_scripted_model_rejects_unexpected_call() -> None:
    model = ScriptedModel()

    with pytest.raises(UnexpectedModelCall, match="no scripted steps remain") as exc_info:
        await Runner.run(Agent(name="test", model=model), "hi")

    assert exc_info.value.call_index == 0
    assert "call #1" in str(exc_info.value)
    assert exc_info.value.call.streamed is False
    assert exc_info.value.call.input == [{"content": "hi", "role": "user"}]

    exc_info.value.call.input[0]["content"] = "changed"
    assert model.calls[0].input == [{"content": "hi", "role": "user"}]


@pytest.mark.asyncio
async def test_scripted_model_unexpected_streaming_call_records_streamed_attribute() -> None:
    model = ScriptedModel()

    with pytest.raises(UnexpectedModelCall) as exc_info:
        async for _event in model.stream_response(
            None,
            [],
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            pass

    assert exc_info.value.call_index == 0
    assert exc_info.value.call.streamed is True


def test_scripted_model_reports_unconsumed_steps() -> None:
    model = ScriptedModel([[assistant_message("unused")]])

    with pytest.raises(UnconsumedModelSteps, match="1 scripted model step") as exc_info:
        model.assert_complete()

    assert exc_info.value.remaining_steps == 1
