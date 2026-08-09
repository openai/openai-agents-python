from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)

from agents import Agent, ComputerTool, InputItem, RunConfig, Runner, function_tool
from agents.exceptions import InputGuardrailTripwireTriggered, ModelBehaviorError, UserError
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import AgentHooks, RunHooks
from agents.run import CallModelData, ModelInputData
from agents.run_context import RunContextWrapper
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.run_internal.run_steps import NextStepInterruption, NextStepRunAgain
from agents.run_state import CURRENT_SCHEMA_VERSION, RunState
from agents.tool import Tool
from agents.usage import Usage

from .fake_model import FakeModel
from .test_computer_tool_lifecycle import FakeComputer
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


def _item_type(item: TResponseInputItem) -> str | None:
    if not isinstance(item, dict):
        return getattr(item, "type", None)
    return cast(str | None, item.get("type") or item.get("role"))


def _message_text(item: TResponseInputItem) -> str | None:
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
        )
    return None


async def _make_after_turn_state(
    *,
    session: SimpleListSession | None = None,
    auto_previous_response_id: bool = False,
) -> tuple[FakeModel, Agent[Any], RunState[Any], list[str]]:
    calls: list[str] = []

    @function_tool(name_override="record_destination")
    def record_destination(destination: str) -> str:
        calls.append(destination)
        return f"recorded:{destination}"

    model = FakeModel()
    model.set_next_output(
        [
            get_function_tool_call(
                "record_destination",
                json.dumps({"destination": "Paris"}),
                call_id="call-destination",
            )
        ]
    )
    agent = Agent(name="assistant", model=model, tools=[record_destination])
    streamed = Runner.run_streamed(
        agent,
        "Initial request",
        session=session,
        auto_previous_response_id=auto_previous_response_id,
    )
    async for event in streamed.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            streamed.cancel(mode="after_turn")

    state = streamed.to_state()
    assert isinstance(state._current_step, NextStepRunAgain)
    assert calls == ["Paris"]
    return model, agent, state, calls


@pytest.mark.asyncio
async def test_pending_input_preserves_order_and_serialization_round_trips() -> None:
    agent = Agent(name="assistant")
    state: RunState[Any] = RunState(
        context=RunContextWrapper(context={}),
        original_input="Initial request",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    starting_turn = state._current_turn

    state.add_input("First late message")
    state.add_input([{"role": "user", "content": "Second late message"}])
    assert state._current_turn == starting_turn

    assert [_message_text(item) for item in state.pending_input] == [
        "First late message",
        "Second late message",
    ]
    detached_view = state.pending_input
    cast(dict[str, Any], detached_view[0])["content"] = "mutated"
    assert _message_text(state.pending_input[0]) == "First late message"

    serialized = state.to_json()
    assert serialized["$schemaVersion"] == CURRENT_SCHEMA_VERSION
    restored = await RunState.from_json(agent, serialized)
    restored_from_string = await RunState.from_string(agent, state.to_string())

    for candidate in (restored, restored_from_string):
        assert isinstance(candidate._current_step, NextStepRunAgain)
        assert [_message_text(item) for item in candidate.pending_input] == [
            "First late message",
            "Second late message",
        ]

    legacy = state.to_json()
    legacy["$schemaVersion"] = "1.14"
    legacy.pop("pending_input")
    legacy["current_step"] = None
    restored_legacy = await RunState.from_json(agent, legacy)
    assert restored_legacy.pending_input == []


@pytest.mark.asyncio
async def test_after_turn_resume_admits_input_after_tool_output_exactly_once() -> None:
    session = SimpleListSession()
    model, agent, state, calls = await _make_after_turn_state(session=session)
    state.add_input("Change the destination to Tokyo")
    model.set_next_output([get_text_message("Updated")])

    result = await Runner.run(agent, state, session=session)

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_item_type(item) for item in model_input] == [
        "user",
        "function_call",
        "function_call_output",
        "user",
    ]
    assert [_message_text(item) for item in model_input].count(
        "Change the destination to Tokyo"
    ) == 1
    assert state.pending_input == []

    session_items = await session.get_items()
    assert [_message_text(item) for item in session_items].count(
        "Change the destination to Tokyo"
    ) == 1
    replay_items = result.to_input_list()
    assert [_message_text(item) for item in replay_items].count(
        "Change the destination to Tokyo"
    ) == 1
    for terminal_state in (state, result.to_state()):
        with pytest.raises(UserError, match="terminal RunState"):
            terminal_state.add_input("Too late")


@pytest.mark.asyncio
async def test_streamed_resume_matches_pending_input_ordering() -> None:
    model, agent, state, calls = await _make_after_turn_state()
    state.add_input("Change the destination to Tokyo")
    model.set_next_output([get_text_message("Updated")])

    result = Runner.run_streamed(agent, state)
    async for _ in result.stream_events():
        pass

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_item_type(item) for item in model_input] == [
        "user",
        "function_call",
        "function_call_output",
        "user",
    ]
    assert [_message_text(item) for item in model_input].count(
        "Change the destination to Tokyo"
    ) == 1
    assert state.pending_input == []
    for terminal_state in (state, result.to_state()):
        with pytest.raises(UserError, match="terminal RunState"):
            terminal_state.add_input("Too late")


@pytest.mark.asyncio
async def test_server_managed_resume_sends_pending_input_as_unsent_delta_once() -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Change the destination to Tokyo")
    model.set_next_output([get_text_message("Updated")])

    result = await Runner.run(agent, state)

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    assert model.last_turn_args["previous_response_id"] == "resp-789"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_item_type(item) for item in model_input] == ["function_call_output", "user"]
    assert [_message_text(item) for item in model_input].count(
        "Change the destination to Tokyo"
    ) == 1
    assert state.pending_input == []


def test_server_tracker_distinguishes_identical_input_occurrences_after_restore() -> None:
    agent = Agent(name="assistant")
    admitted_first = InputItem(
        agent=agent,
        raw_item={"role": "user", "content": "Repeat"},
    )
    admitted_second = InputItem(
        agent=agent,
        raw_item={"role": "user", "content": "Repeat"},
    )
    tracker = OpenAIServerConversationTracker(previous_response_id="resp-latest")
    tracker.hydrate_from_state(
        original_input="Initial request",
        generated_items=[admitted_first],
        model_responses=[ModelResponse(output=[], usage=Usage(), response_id="resp-latest")],
    )

    assert tracker.prepare_input("Initial request", [admitted_first, admitted_second]) == [
        admitted_second.raw_item
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_second_resume", [False, True])
async def test_server_managed_resume_sends_identical_late_input_in_later_occurrence(
    streamed_second_resume: bool,
) -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Repeat")
    model.set_next_output(
        [
            get_function_tool_call(
                "record_destination",
                json.dumps({"destination": "Rome"}),
                call_id="call-second-destination",
            )
        ]
    )

    first_resume = Runner.run_streamed(agent, state)
    async for event in first_resume.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            first_resume.cancel(mode="after_turn")

    state = await RunState.from_json(agent, first_resume.to_state().to_json())
    admitted_before = next(item for item in state._generated_items if isinstance(item, InputItem))
    state.add_input("Repeat")
    model.set_next_output([get_text_message("Done")])

    if streamed_second_resume:
        streamed_result = Runner.run_streamed(agent, state)
        async for _event in streamed_result.stream_events():
            pass
        final_output = streamed_result.final_output
    else:
        run_result = await Runner.run(agent, state)
        final_output = run_result.final_output

    assert final_output == "Done"
    assert calls == ["Paris", "Rome"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Repeat") == 1
    admitted_after = [item for item in state._generated_items if isinstance(item, InputItem)]
    assert [item.input_id for item in admitted_after].count(admitted_before.input_id) == 1
    assert len({item.input_id for item in admitted_after}) == 2


@pytest.mark.asyncio
async def test_unresolved_approval_keeps_pending_input_until_tool_finishes() -> None:
    calls: list[str] = []

    @function_tool(needs_approval=True)
    def protected_tool(value: str) -> str:
        calls.append(value)
        return f"approved:{value}"

    model = FakeModel()
    model.set_next_output(
        [get_function_tool_call("protected_tool", '{"value":"one"}', call_id="call-protected")]
    )
    agent = Agent(name="assistant", model=model, tools=[protected_tool])
    interrupted = await Runner.run(agent, "Initial request")
    state = interrupted.to_state()
    state.add_input("Late input")

    still_interrupted = await Runner.run(agent, state)
    assert still_interrupted.interruptions
    assert calls == []
    assert _message_text(state.pending_input[0]) == "Late input"

    state.approve(state.get_interruptions()[0])
    model.set_next_output([get_text_message("Done")])
    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "Done"
    assert calls == ["one"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_item_type(item) for item in model_input][-2:] == ["function_call_output", "user"]
    assert _message_text(model_input[-1]) == "Late input"


@pytest.mark.asyncio
async def test_streamed_after_turn_cancel_keeps_pending_input_for_next_resume() -> None:
    calls: list[str] = []

    @function_tool(needs_approval=True)
    def protected_tool(value: str) -> str:
        calls.append(value)
        return f"approved:{value}"

    model = FakeModel()
    model.set_next_output(
        [get_function_tool_call("protected_tool", '{"value":"one"}', call_id="call-protected")]
    )
    agent = Agent(name="assistant", model=model, tools=[protected_tool])
    interrupted = await Runner.run(agent, "Initial request")
    state = interrupted.to_state()
    state.add_input("Late input")
    state.approve(state.get_interruptions()[0])

    resumed = Runner.run_streamed(agent, state)
    async for event in resumed.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            resumed.cancel(mode="after_turn")

    assert calls == ["one"]
    assert _message_text(state.pending_input[0]) == "Late input"

    model.set_next_output([get_text_message("Done")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Done"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "tool_use_behavior",
    [
        "stop_on_first_tool",
        {"stop_at_tool_names": ["protected_tool"]},
        lambda _context, _results: None,
    ],
)
async def test_interruption_without_guaranteed_next_model_rejects_input(
    streamed: bool,
    tool_use_behavior: Any,
) -> None:
    @function_tool(needs_approval=True)
    def protected_tool(value: str) -> str:
        return value

    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "protected_tool",
                '{"value":"one"}',
                call_id="call-protected-terminal",
            )
        ]
    )
    agent = Agent(
        name="assistant",
        model=model,
        tools=[protected_tool],
        tool_use_behavior=cast(Any, tool_use_behavior),
    )
    if streamed:
        interrupted_stream = Runner.run_streamed(agent, "Initial request")
        async for _event in interrupted_stream.stream_events():
            pass
        state = interrupted_stream.to_state()
    else:
        interrupted = await Runner.run(agent, "Initial request")
        state = interrupted.to_state()

    before = state.to_json()
    with pytest.raises(UserError, match="tool result may end the run"):
        state.add_input("Late input")
    assert state.to_json() == before


@pytest.mark.asyncio
async def test_pending_input_guardrail_trip_keeps_input_recoverable() -> None:
    model, agent, state, _calls = await _make_after_turn_state()
    guarded_inputs: list[list[TResponseInputItem]] = []

    def trip_pending_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        guarded_inputs.append(cast(list[TResponseInputItem], input))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent.input_guardrails = [InputGuardrail(guardrail_function=trip_pending_input)]
    state.add_input("Unsafe late input")
    model.set_next_output([get_text_message("Must not run")])
    queued_outputs = len(model.turn_outputs)

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, state)

    assert len(model.turn_outputs) == queued_outputs
    assert [[_message_text(item) for item in batch] for batch in guarded_inputs] == [
        ["Unsafe late input"]
    ]
    assert _message_text(state.pending_input[0]) == "Unsafe late input"
    state.clear_pending_input()
    assert state.pending_input == []


@pytest.mark.asyncio
async def test_pending_input_runs_agent_and_run_config_guardrails_on_only_pending() -> None:
    model, agent, state, _calls = await _make_after_turn_state()
    guarded_inputs: list[tuple[str, list[TResponseInputItem]]] = []

    def inspect_agent_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        guarded_inputs.append(("agent", cast(list[TResponseInputItem], input)))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    def inspect_config_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        guarded_inputs.append(("config", cast(list[TResponseInputItem], input)))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent.input_guardrails = [InputGuardrail(guardrail_function=inspect_agent_input)]
    run_config = RunConfig(
        input_guardrails=[InputGuardrail(guardrail_function=inspect_config_input)]
    )
    state.add_input("Guard only this")
    model.set_next_output([get_text_message("Done")])

    result = await Runner.run(agent, state, run_config=run_config)

    assert result.final_output == "Done"
    assert {source for source, _batch in guarded_inputs} == {"agent", "config"}
    assert [[_message_text(item) for item in batch] for _source, batch in guarded_inputs] == [
        ["Guard only this"],
        ["Guard only this"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_retry", [False, True])
async def test_guardrail_retry_persists_successful_turn_with_session(
    streamed_retry: bool,
) -> None:
    session = SimpleListSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    should_trip = True

    def inspect_pending_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=should_trip)

    agent.input_guardrails = [InputGuardrail(guardrail_function=inspect_pending_input)]
    state.add_input("Late input")

    if streamed_retry:
        tripped = Runner.run_streamed(agent, state, session=session)
        with pytest.raises(InputGuardrailTripwireTriggered):
            async for _event in tripped.stream_events():
                pass
    else:
        with pytest.raises(InputGuardrailTripwireTriggered):
            await Runner.run(agent, state, session=session)

    should_trip = False
    model.set_next_output([get_text_message("Recovered")])
    if streamed_retry:
        streamed_result = Runner.run_streamed(agent, state, session=session)
        async for _event in streamed_result.stream_events():
            pass
        final_output = streamed_result.final_output
    else:
        run_result = await Runner.run(agent, state, session=session)
        final_output = run_result.final_output

    assert final_output == "Recovered"
    session_items = await session.get_items()
    assert [_message_text(item) for item in session_items].count("Late input") == 1
    assert _item_type(session_items[-1]) == "message"
    assert cast(dict[str, Any], session_items[-1]).get("role") == "assistant"
    assert [result.output.tripwire_triggered for result in state._input_guardrail_results] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_failed_model_request_does_not_duplicate_admitted_input_on_resume() -> None:
    model, agent, state, _calls = await _make_after_turn_state()
    state.add_input("Late input")
    model.set_next_output(RuntimeError("model failed"))

    with pytest.raises(RuntimeError, match="model failed"):
        await Runner.run(agent, state)

    assert state.pending_input == []
    admitted_items = [item for item in state._generated_items if isinstance(item, InputItem)]
    assert [_message_text(item.raw_item) for item in admitted_items] == ["Late input"]
    admitted_input_id = admitted_items[0].input_id

    state = await RunState.from_json(agent, state.to_json())
    assert (
        next(item.input_id for item in state._generated_items if isinstance(item, InputItem))
        == admitted_input_id
    )
    model.set_next_output([get_text_message("Recovered")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1


@pytest.mark.asyncio
async def test_failed_model_request_with_session_persists_admitted_input_once() -> None:
    session = SimpleListSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input("Late input")
    model.set_next_output(RuntimeError("model failed"))

    with pytest.raises(RuntimeError, match="model failed"):
        await Runner.run(agent, state, session=session)

    assert state.pending_input == []
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == 1

    state = await RunState.from_json(agent, state.to_json())
    model.set_next_output([get_text_message("Recovered")])
    result = await Runner.run(agent, state, session=session)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == 1


@pytest.mark.asyncio
async def test_failed_server_managed_request_keeps_pending_input_for_retry() -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.set_next_output(RuntimeError("model failed"))

    with pytest.raises(RuntimeError, match="model failed"):
        await Runner.run(agent, state)

    assert _message_text(state.pending_input[0]) == "Late input"
    state = await RunState.from_json(agent, state.to_json())
    model.set_next_output([get_text_message("Recovered")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert state.pending_input == []


@pytest.mark.asyncio
async def test_server_filter_omission_remains_pending_for_later_nonstream_turn() -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "record_destination",
                    json.dumps({"destination": "Rome"}),
                    call_id="call-filtered-destination",
                )
            ],
            [get_text_message("Done")],
        ]
    )
    filter_calls = 0

    def omit_first_request(data: CallModelData[Any]) -> ModelInputData:
        nonlocal filter_calls
        filter_calls += 1
        return ModelInputData(
            input=[] if filter_calls == 1 else data.model_data.input,
            instructions=data.model_data.instructions,
        )

    result = await Runner.run(
        agent,
        state,
        run_config=RunConfig(call_model_input_filter=omit_first_request),
    )

    assert result.final_output == "Done"
    assert calls == ["Paris", "Rome"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert state.pending_input == []


@pytest.mark.asyncio
async def test_server_filter_omission_survives_streamed_state_round_trip() -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.set_next_output(
        [
            get_function_tool_call(
                "record_destination",
                json.dumps({"destination": "Rome"}),
                call_id="call-filtered-destination",
            )
        ]
    )

    def omit_pending(data: CallModelData[Any]) -> ModelInputData:
        return ModelInputData(input=[], instructions=data.model_data.instructions)

    filtered = Runner.run_streamed(
        agent,
        state,
        run_config=RunConfig(call_model_input_filter=omit_pending),
    )
    async for event in filtered.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            filtered.cancel(mode="after_turn")

    state = await RunState.from_json(agent, filtered.to_state().to_json())
    assert [_message_text(item) for item in state.pending_input] == ["Late input"]
    assert not any(isinstance(item, InputItem) for item in state._generated_items)

    model.set_next_output([get_text_message("Done")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Done"
    assert calls == ["Paris", "Rome"]
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Late input") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_server_filter_reconstructed_pending_rewrite_is_rejected(streamed: bool) -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.set_next_output([get_text_message("Done")])

    def reconstruct_pending(data: CallModelData[Any]) -> ModelInputData:
        rewritten = [
            {"role": "user", "content": "Filtered late input"}
            if _message_text(item) == "Late input"
            else item
            for item in data.model_data.input
        ]
        return ModelInputData(
            input=cast(list[TResponseInputItem], rewritten),
            instructions=data.model_data.instructions,
        )

    queued_outputs = len(model.turn_outputs)
    run_config = RunConfig(call_model_input_filter=reconstruct_pending)
    if streamed:
        failed = Runner.run_streamed(agent, state, run_config=run_config)
        with pytest.raises(UserError, match="cannot safely associate"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(UserError, match="cannot safely associate"):
            await Runner.run(agent, state, run_config=run_config)

    assert len(model.turn_outputs) == queued_outputs
    assert [_message_text(item) for item in state.pending_input] == ["Late input"]


@pytest.mark.asyncio
async def test_server_filter_in_place_pending_rewrite_preserves_occurrence() -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.set_next_output([get_text_message("Done")])

    def rewrite_pending_in_place(data: CallModelData[Any]) -> ModelInputData:
        for item in data.model_data.input:
            if isinstance(item, dict) and _message_text(item) == "Late input":
                cast(dict[str, Any], item)["content"] = "Filtered late input"
        return data.model_data

    result = await Runner.run(
        agent,
        state,
        run_config=RunConfig(call_model_input_filter=rewrite_pending_in_place),
    )

    assert result.final_output == "Done"
    model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in model_input].count("Filtered late input") == 1
    assert state.pending_input == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_failure", [False, True])
async def test_server_response_acceptance_commits_before_hook_failure(
    streamed_failure: bool,
) -> None:
    class CountAgentResponseHook(AgentHooks[Any]):
        def __init__(self) -> None:
            self.call_count = 0

        async def on_llm_end(
            self,
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            _response: ModelResponse,
        ) -> None:
            self.call_count += 1

    class FailAfterResponse(RunHooks[Any]):
        async def on_llm_end(
            self,
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            _response: ModelResponse,
        ) -> None:
            raise RuntimeError("after response")

    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    agent_hooks = CountAgentResponseHook()
    agent.hooks = agent_hooks
    state.add_input("Late input")
    model.set_next_output([get_text_message("Accepted")])

    if streamed_failure:
        failed = Runner.run_streamed(agent, state, hooks=FailAfterResponse())
        with pytest.raises(RuntimeError, match="after response"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(RuntimeError, match="after response"):
            await Runner.run(agent, state, hooks=FailAfterResponse())

    accepted_model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in accepted_model_input].count("Late input") == 1
    assert state.pending_input == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted
    assert state._current_step.llm_end_hooks_started
    assert agent_hooks.call_count == 1
    state = await RunState.from_json(agent, state.to_json())
    queued_outputs = len(model.turn_outputs)

    recovered = await Runner.run(agent, state)
    assert recovered.final_output == "Accepted"
    assert agent_hooks.call_count == 1
    assert len(model.turn_outputs) == queued_outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_failure", [False, True])
async def test_server_acceptance_commits_before_invocation_validation_failure(
    streamed_failure: bool,
) -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.set_next_output(
        [
            get_function_tool_call(
                "record_destination",
                json.dumps({"destination": "Rome"}),
                call_id="call-destination",
            )
        ]
    )

    if streamed_failure:
        failed = Runner.run_streamed(agent, state)
        with pytest.raises(ModelBehaviorError, match="completed tool call ID"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(ModelBehaviorError, match="completed tool call ID"):
            await Runner.run(agent, state)

    accepted_model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in accepted_model_input].count("Late input") == 1
    assert state.pending_input == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted
    assert state._last_processed_response is None
    assert calls == ["Paris"]

    state = await RunState.from_json(agent, state.to_json())
    queued_outputs = len(model.turn_outputs)
    with pytest.raises(UserError, match="accepted model response could not be processed"):
        await Runner.run(agent, state)
    assert len(model.turn_outputs) == queued_outputs
    assert calls == ["Paris"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_failure", [False, True])
async def test_server_accepted_computer_start_hook_failure_is_not_replayed(
    streamed_failure: bool,
) -> None:
    screenshots: list[str] = []

    class RecordingComputer(FakeComputer):
        def screenshot(self) -> str:
            screenshots.append("screenshot")
            return "img"

    class FailComputerStart(RunHooks[Any]):
        def __init__(self) -> None:
            self.call_count = 0

        async def on_tool_start(
            self,
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            tool: Tool,
        ) -> None:
            if isinstance(tool, ComputerTool):
                self.call_count += 1
                raise RuntimeError("computer hook failed")

    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    agent.tools = [ComputerTool(computer=RecordingComputer())]
    state.add_input("Late input")
    model.set_next_output(
        [
            ResponseComputerToolCall(
                id="computer-item",
                type="computer_call",
                action=ActionScreenshot(type="screenshot"),
                call_id="computer-call",
                pending_safety_checks=[],
                status="completed",
            )
        ]
    )
    hooks = FailComputerStart()

    if streamed_failure:
        failed = Runner.run_streamed(agent, state, hooks=hooks)
        with pytest.raises(RuntimeError, match="computer hook failed"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(RuntimeError, match="computer hook failed"):
            await Runner.run(agent, state, hooks=hooks)

    assert hooks.call_count == 1
    assert screenshots == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted

    state = await RunState.from_json(agent, state.to_json())
    with pytest.raises(ModelBehaviorError, match="output was not committed"):
        await Runner.run(agent, state)
    assert hooks.call_count == 1
    assert screenshots == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_failure", [False, True])
@pytest.mark.parametrize("failure_phase", ["start", "end"])
async def test_server_accepted_tool_side_effect_failure_is_safe(
    streamed_failure: bool,
    failure_phase: str,
) -> None:
    class FailToolHook(RunHooks[Any]):
        async def on_tool_start(
            self,
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            _tool: Tool,
        ) -> None:
            if failure_phase == "start":
                raise RuntimeError("tool hook failed")

        async def on_tool_end(
            self,
            _context: RunContextWrapper[Any],
            _agent: Agent[Any],
            _tool: Tool,
            _result: object,
        ) -> None:
            if failure_phase == "end":
                raise RuntimeError("tool hook failed")

    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "record_destination",
                    json.dumps({"destination": "Rome"}),
                    call_id="call-retry-destination",
                )
            ],
            [get_text_message("Recovered")],
        ]
    )

    if streamed_failure:
        failed = Runner.run_streamed(agent, state, hooks=FailToolHook())
        with pytest.raises(UserError, match="tool hook failed"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(UserError, match="tool hook failed"):
            await Runner.run(agent, state, hooks=FailToolHook())

    assert state.pending_input == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted
    assert state._current_step.llm_end_hooks_started
    assert calls == (["Paris"] if failure_phase == "start" else ["Paris", "Rome"])

    state = await RunState.from_json(agent, state.to_json())
    if failure_phase == "start":
        with pytest.raises(ModelBehaviorError, match="output was not committed"):
            await Runner.run(agent, state)
        assert calls == ["Paris"]
        return

    recovered = await Runner.run(agent, state)
    assert recovered.final_output == "Recovered"
    assert calls == ["Paris", "Rome"]
    retry_model_input = cast(list[TResponseInputItem], model.last_turn_args["input"])
    assert [_message_text(item) for item in retry_model_input].count("Late input") == 0


@pytest.mark.asyncio
async def test_terminal_state_rejects_pending_input_without_mutation() -> None:
    model = FakeModel(initial_output=[get_text_message("Done")])
    agent = Agent(name="assistant", model=model)
    result = await Runner.run(agent, "Initial request")
    state = result.to_state()
    before = state.to_json()

    with pytest.raises(UserError, match="terminal RunState"):
        state.add_input("Too late")

    assert state.to_json() == before
