"""Consume the migration result and stream through the public SDK types."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseOutputText,
    ResponseReasoningItem,
)
from openai.types.responses.response_input_item_param import FunctionCallOutput
from openai.types.responses.response_output_text import AnnotationURLCitation
from typing_extensions import assert_type

from agents import Allow, Deny
from agents.classic import Agent, RunContextWrapper, function_tool
from agents.classic.items import (
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.classic.stream_events import AgentUpdatedStreamEvent, RunItemStreamEvent, StreamEvent
from agents.classic.tool import ToolOriginType
from agents.classic.tool_context import ToolContext
from agents.classic.usage import Usage
from agents.migration import SDKRunHandle, SDKRunner, SDKRunResult

from .test_migration_runner import (
    ScriptedModel,
    call,
    local_transport_bypass as local_transport_bypass,
    message,
)


class ResultModel(ScriptedModel):
    first_raw_usage = {"input_tokens": 7, "output_tokens_details": {"reasoning_tokens": 2}}

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        response = await super().get_response(*args, **kwargs)
        # Exercise a provider that does not supply the final response ID.
        if len(self.requests) == 2:
            response.response_id = None
        else:
            response.raw_usage = self.first_raw_usage
            kwargs["tools"][0].description = "Edited by the Model after admission."
        response.request_id = f"req_{len(self.requests)}"
        return response


async def consume_events(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in events]


@pytest.mark.parametrize("streamed", [False, True])
async def test_consumers_read_observed_items_usage_and_detached_history(streamed: bool) -> None:
    context = {"greeting": "Hello"}
    effect_log: list[str] = []
    quoted_text = 'Hello, "Ada"!\nSecond line.'

    @function_tool
    def greet(wrapper: RunContextWrapper[dict[str, str]], value: str) -> str:
        """Greet a person by name."""
        assert wrapper.context is context
        effect_log.append(value)
        return f'{wrapper.context["greeting"]}, "{value}"!\nSecond line.'

    @function_tool
    def empty(value: str) -> str:
        effect_log.append(value)
        return ""

    reasoning = ResponseReasoningItem.model_validate(
        {
            "id": "rs_observed",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "synthetic summary"}],
            "content": [{"type": "reasoning_text", "text": "synthetic reasoning"}],
            "encrypted_content": "synthetic-encrypted",
        }
    )
    interim = message("Calling greet and empty")
    interim.id = "msg_interim"
    interim.phase = "commentary"
    text = interim.content[0]
    assert isinstance(text, ResponseOutputText)
    text.annotations.append(
        AnnotationURLCitation(
            type="url_citation",
            url="https://example.com/greeting",
            title="Synthetic citation",
            start_index=0,
            end_index=7,
        )
    )
    greet_call = call("greet", arguments=' { "value": "Ada" } ')
    empty_call = call("empty", call_id="call_empty", arguments='{"value":"empty"}')
    empty_call.id = None
    final = message("Finished.")
    final.id = "msg_final"
    first_output = [reasoning, interim, greet_call, empty_call]
    model = ResultModel(first_output, [final])
    agent: Agent[dict[str, str]] = Agent("Greeter", model=model, tools=[greet, empty])

    if streamed:
        handle = SDKRunner.start(agent, "Greet Ada.", context=context)
        assert_type(handle, SDKRunHandle[dict[str, str]])
        events = await consume_events(handle.replay_events())
        assert handle.is_complete
        assert handle.final_output == "Finished."
        result = await handle.get_result()
    else:
        result = await SDKRunner.run(agent, "Greet Ada.", context=context)
        events = await consume_events(result.replay_events())
    assert_type(result, SDKRunResult[dict[str, str]])
    assert_type(result.new_items, list[RunItem])
    assert_type(result.raw_responses, list[ModelResponse])
    context_wrapper = result.context_wrapper
    assert context_wrapper is not None
    assert_type(context_wrapper, RunContextWrapper[dict[str, str] | None])
    assert_type(context_wrapper.usage, Usage)

    assert result.final_output == result.native_result.final_output == "Finished."
    assert result.native_result.turn.turn_id
    assert result.last_agent is agent
    assert context_wrapper.context is context
    assert result.interruptions == []
    assert sorted(effect_log) == ["Ada", "empty"]
    assert len(result.raw_responses) == 2
    assert [response.request_id for response in result.raw_responses] == ["req_1", "req_2"]
    assert result.raw_responses[0].output == first_output
    assert result.raw_responses[1].output == [final]
    assert [response.response_id for response in result.raw_responses] == ["response_1", None]
    assert result.raw_responses[0].raw_usage == model.first_raw_usage
    assert result.raw_responses[0].raw_usage is not model.first_raw_usage
    assert result.raw_responses[1].raw_usage is None
    assert result.last_response_id is None
    usage = context_wrapper.usage
    assert (usage.requests, usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
        2,
        14,
        6,
        20,
    )

    reason_item, interim_item, greet_item, empty_item, first_receipt, second_receipt, final_item = (
        result.new_items
    )
    assert isinstance(reason_item, ReasoningItem) and reason_item.raw_item == reasoning
    assert isinstance(interim_item, MessageOutputItem) and interim_item.raw_item == interim
    assert isinstance(greet_item, ToolCallItem) and greet_item.raw_item == greet_call
    assert greet_item.description == "Greet a person by name."
    assert isinstance(empty_item, ToolCallItem) and empty_item.raw_item == empty_call
    assert isinstance(empty_item.raw_item, ResponseFunctionToolCall)
    assert empty_item.raw_item.id is None
    assert greet_item.tool_origin is not None
    assert greet_item.tool_origin.type is ToolOriginType.FUNCTION
    assert empty_item.tool_origin == greet_item.tool_origin
    assert isinstance(final_item, MessageOutputItem) and final_item.raw_item == final
    assert all(item.agent is agent for item in result.new_items)
    expected_receipts = [
        {key: value for key, value in item.items() if key != "id"}
        for item in model.requests[1]["input"]
        if item["type"] == "function_call_output"
    ]
    assert isinstance(first_receipt, ToolCallOutputItem)
    assert isinstance(second_receipt, ToolCallOutputItem)
    assert [first_receipt.raw_item, second_receipt.raw_item] == expected_receipts
    assert all(
        item.tool_origin == greet_item.tool_origin for item in (first_receipt, second_receipt)
    )
    assert {item.call_id: item.output for item in (first_receipt, second_receipt)} == {
        greet_call.call_id: quoted_text,
        empty_call.call_id: "",
    }

    expected = [
        {"role": "user", "content": "Greet Ada."},
        reasoning.model_dump(exclude_unset=True),
        interim.model_dump(exclude_unset=True),
        greet_call.model_dump(exclude_unset=True),
        empty_call.model_dump(exclude_unset=True),
        *expected_receipts,
        final.model_dump(exclude_unset=True),
    ]
    history = result.to_input_list()
    assert history == result.to_input_list(mode="normalized") == expected
    changed_message = cast(ResponseOutputMessageParam, history[2])
    changed_text = next(iter(changed_message["content"]))
    assert changed_text["type"] == "output_text"
    changed_text["text"] = "changed message"
    cast(FunctionCallOutput, history[5])["output"] = "changed receipt"
    text.annotations.clear()
    reasoning.summary.clear()
    assert result.to_input_list() == expected
    snapshot = result.raw_responses[0].output[1]
    assert isinstance(snapshot, ResponseOutputMessage)
    assert isinstance(snapshot.content[0], ResponseOutputText)
    assert len(snapshot.content[0].annotations) == 1

    assert events[0] == AgentUpdatedStreamEvent(new_agent=agent)
    item_events = [event for event in events[1:] if isinstance(event, RunItemStreamEvent)]
    assert len(item_events) == len(events) - 1
    assert [event.name for event in item_events] == [
        "reasoning_item_created",
        "message_output_created",
        "tool_called",
        "tool_called",
        "tool_output",
        "tool_output",
        "message_output_created",
    ]
    assert [event.item for event in item_events] == result.new_items


async def test_result_retains_three_positional_arguments() -> None:
    agent: Agent[None] = Agent("Model only", model=ScriptedModel([message("complete")]))
    run = await SDKRunner.run(agent, "hello")

    result = SDKRunResult(run.final_output, agent, run.native_result)
    assert result.final_output == "complete"
    assert result.last_agent is agent
    assert result.native_result is run.native_result
    assert result.to_input_list() == []
    assert result.new_items == []
    assert result.raw_responses == []
    assert result.last_response_id is None
    assert result.context_wrapper is None


async def test_stream_can_settle_without_a_reader_and_reaches_eof() -> None:
    agent = Agent("Model only", model=ScriptedModel([message("complete")]))
    handle = SDKRunner.start(agent, "hello")
    result = await handle.get_result()
    assert handle.is_complete
    assert result.final_output == "complete"
    events = handle.replay_events()
    assert isinstance(await anext(events), AgentUpdatedStreamEvent)
    assert isinstance(await anext(events), RunItemStreamEvent)
    with pytest.raises(StopAsyncIteration):
        await anext(events)


@pytest.mark.parametrize("allow", [True, False], ids=["allow", "deny"])
async def test_stream_buffers_while_tool_approval_is_held(allow: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []
    original_call = call("record", arguments=' { "value": "Ada" } ')
    tool_text = 'Recorded "Ada".\nSecond line.'
    reason = "application denied"

    @function_tool(needs_approval=True)
    async def record(value: str) -> str:
        effects.append(value)
        return tool_text

    async def approve(ctx: ToolContext[str]) -> Allow | Deny:
        assert ctx.tool_call == original_call
        assert ctx.context == "app"
        entered.set()
        await release.wait()
        return Allow() if allow else Deny(reason)

    model = ScriptedModel([original_call], [message("finished")])
    replacement = ScriptedModel([message("must not run")])
    agent: Agent[str] = Agent("Approval", model=model, tools=[record])
    handle = SDKRunner.start(
        agent, "record Ada", context="app", on_approval=approve, return_state=True
    )
    events = handle.replay_events()
    reader = asyncio.ensure_future(anext(events))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert not reader.done()
        assert not handle.is_complete
        assert effects == []
        assert len(model.requests) == 1
        agent.model = replacement
        release.set()
        assert await asyncio.wait_for(reader, 15) == AgentUpdatedStreamEvent(new_agent=agent)
        item_events = await consume_events(events)
    finally:
        handle.cancel()
        release.set()
        await asyncio.gather(reader, return_exceptions=True)
        await asyncio.gather(handle.get_result(), return_exceptions=True)

    result = await handle.get_result()
    assert result.final_output == "finished"
    assert result.interruptions == []
    checkpoint = result.native_result.state
    assert checkpoint is not None and checkpoint.native_continuation is None
    assert len(model.requests) == 2
    assert replacement.requests == []
    assert effects == (["Ada"] if allow else [])
    called, output, completed = item_events
    assert isinstance(called, RunItemStreamEvent) and called.name == "tool_called"
    assert isinstance(completed, RunItemStreamEvent) and completed.name == "message_output_created"
    assert isinstance(output, RunItemStreamEvent) and output.name == "tool_output"
    assert isinstance(output.item, ToolCallOutputItem)
    assert output.item.call_id == original_call.call_id
    [receipt] = [
        item for item in model.requests[1]["input"] if item["type"] == "function_call_output"
    ]
    assert output.item.raw_item == receipt
    if allow:
        assert output.item.output == tool_text
    else:
        assert reason in output.item.output
    assert [event.item for event in (called, output, completed)] == result.new_items


async def test_stream_failure_raises_without_replaying_partial_items() -> None:
    effects: list[str] = []
    failure = RuntimeError("synthetic model failure")

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    class FailingModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            if self.requests:
                raise failure
            return await super().get_response(*args, **kwargs)

    model = FailingModel([call("greet")])
    handle = SDKRunner.start(Agent("Failing", model=model, tools=[greet]), "hello")
    with pytest.raises(RuntimeError) as raised:
        await anext(handle.replay_events())
    assert raised.value is failure
    assert handle.is_complete
    assert effects == ["Ada"]
    with pytest.raises(RuntimeError) as raised:
        await handle.get_result()
    assert raised.value is failure


async def test_stream_cancel_before_first_model_call() -> None:
    model = ScriptedModel([message("should not sample")])
    handle = SDKRunner.start(Agent("Cancelled", model=model), "hello")
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await anext(handle.replay_events())
    assert handle.is_complete
    assert model.requests == []


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_stream_cancellation_joins_started_tool(cancel_waiter: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    @function_tool
    async def greet(value: str) -> str:
        started.set()
        await release.wait()
        finished.set()
        return value

    model = ScriptedModel([call("greet")], [message("should not sample")])
    handle = SDKRunner.start(Agent("Cancellable", model=model, tools=[greet]), "hello")
    reader = asyncio.ensure_future(anext(handle.replay_events()))
    try:
        await asyncio.wait_for(started.wait(), 10)
        assert handle.final_output is None
        if cancel_waiter:
            reader.cancel()
        else:
            handle.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), 0.05)
        handle.cancel()
        assert not handle.is_complete
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, 10)
    assert handle.is_complete
    assert finished.is_set()
    assert len(model.requests) == 1
    with pytest.raises(asyncio.CancelledError):
        await handle.get_result()
