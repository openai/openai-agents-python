from __future__ import annotations

import copy
import json
from typing import Any, Literal, cast

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
from agents.memory import OpenAIConversationsSession, Session
from agents.run import CallModelData, ModelInputData
from agents.run_context import RunContextWrapper
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.run_internal.run_steps import NextStepInterruption, NextStepRunAgain
from agents.run_internal.session_persistence import resume_pending_session_write
from agents.run_state import CURRENT_SCHEMA_VERSION, RunState
from agents.testing import ScriptedModel
from agents.tool import Tool
from agents.usage import Usage

from .model_test_helpers import get_exact_output_stream_step
from .test_computer_tool_lifecycle import FakeComputer
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


class _PendingInputWriteFailureSession(SimpleListSession):
    def __init__(self) -> None:
        super().__init__()
        self.failure: Literal["before", "after"] | None = None
        self.error = RuntimeError("pending input Session append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error
        await super().add_items(items)
        if failure == "after":
            raise self.error


class _CheckpointOpenAIConversationsSession(OpenAIConversationsSession):
    def __init__(self, session_id: str | None = "test") -> None:
        self._session_id = session_id
        self.items: list[TResponseInputItem] = []
        self.initializations = 0

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if self._session_id is None:
            self._session_id = "lazy-test"
            self.initializations += 1
        if limit == 0:
            return []
        return list(self.items if limit is None else self.items[-limit:])

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.items.extend(copy.deepcopy(items))


class _NormalizingOpenAIConversationsSession(_CheckpointOpenAIConversationsSession):
    def __init__(self) -> None:
        super().__init__()
        self.failure: Literal["before", "after"] | None = None
        self.error = RuntimeError("normalized Conversations append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error

        normalized_items: list[TResponseInputItem] = []
        for item in copy.deepcopy(items):
            if isinstance(item, dict) and item.get("type") == "function_call":
                normalized_item = cast(dict[str, Any], item)
                normalized_item["id"] = f"fc_{len(self.items) + len(normalized_items)}"
                normalized_item["status"] = "completed"
                normalized_item["created_by"] = "server"
                normalized_items.append(cast(TResponseInputItem, normalized_item))
                continue
            if not isinstance(item, dict) or item.get("role") not in {
                "user",
                "assistant",
                "system",
                "developer",
            }:
                normalized_items.append(item)
                continue

            normalized_item = cast(dict[str, Any], item)
            normalized_item["id"] = f"msg_{len(self.items) + len(normalized_items)}"
            normalized_item["type"] = "message"
            normalized_item["status"] = "completed"
            normalized_item["phase"] = None
            content = normalized_item.get("content")
            if isinstance(content, str):
                if normalized_item["role"] == "assistant":
                    normalized_item["content"] = [
                        {
                            "type": "output_text",
                            "text": content,
                            "annotations": [],
                            "logprobs": None,
                        }
                    ]
                else:
                    normalized_item["content"] = [
                        {
                            "type": "input_text",
                            "text": content,
                            "prompt_cache_breakpoint": None,
                        }
                    ]
            normalized_items.append(cast(TResponseInputItem, normalized_item))

        self.items.extend(normalized_items)
        if failure == "after":
            raise self.error


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


def _assistant_message_text(item: TResponseInputItem) -> str | None:
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
    return None


async def _make_after_turn_state(
    *,
    session: Session | None = None,
    auto_previous_response_id: bool = False,
) -> tuple[ScriptedModel, Agent[Any], RunState[Any], list[str]]:
    calls: list[str] = []

    @function_tool(name_override="record_destination")
    def record_destination(destination: str) -> str:
        calls.append(destination)
        return f"recorded:{destination}"

    model = ScriptedModel()
    model.enqueue(
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


async def _resume_pending_input_state(
    agent: Agent[Any],
    state: RunState[Any],
    session: Session,
    *,
    streamed: bool,
) -> Any:
    run_config = RunConfig(tracing_disabled=True)
    if not streamed:
        return await Runner.run(agent, state, session=session, run_config=run_config)

    result = Runner.run_streamed(agent, state, session=session, run_config=run_config)
    async for _event in result.stream_events():
        pass
    return result


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
    model.enqueue([get_text_message("Updated")])

    result = await Runner.run(agent, state, session=session)

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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
    model.enqueue([get_text_message("Updated")])

    result = Runner.run_streamed(agent, state)
    async for _ in result.stream_events():
        pass

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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
    model.enqueue([get_text_message("Updated")])

    result = await Runner.run(agent, state)

    assert result.final_output == "Updated"
    assert calls == ["Paris"]
    assert model.calls[-1].previous_response_id == "resp-789"
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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
    model.enqueue(
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
    model.enqueue([get_text_message("Done")])

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
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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

    model = ScriptedModel()
    model.enqueue(
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
    model.enqueue([get_text_message("Done")])
    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "Done"
    assert calls == ["one"]
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_item_type(item) for item in model_input][-2:] == ["function_call_output", "user"]
    assert _message_text(model_input[-1]) == "Late input"


@pytest.mark.asyncio
async def test_streamed_after_turn_cancel_keeps_pending_input_for_next_resume() -> None:
    calls: list[str] = []

    @function_tool(needs_approval=True)
    def protected_tool(value: str) -> str:
        calls.append(value)
        return f"approved:{value}"

    model = ScriptedModel()
    model.enqueue(
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

    model.enqueue([get_text_message("Done")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Done"
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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

    model = ScriptedModel(
        steps=[
            [
                get_function_tool_call(
                    "protected_tool",
                    '{"value":"one"}',
                    call_id="call-protected-terminal",
                )
            ]
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
    model.enqueue([get_text_message("Must not run")])
    queued_outputs = model.remaining_steps

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, state)

    assert model.remaining_steps == queued_outputs
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
    model.enqueue([get_text_message("Done")])

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
    model.enqueue([get_text_message("Recovered")])
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
    model.enqueue(RuntimeError("model failed"))

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
    model.enqueue([get_text_message("Recovered")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1


@pytest.mark.asyncio
async def test_failed_model_request_with_session_persists_admitted_input_once() -> None:
    session = SimpleListSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input("Late input")
    model.enqueue(RuntimeError("model failed"))

    with pytest.raises(RuntimeError, match="model failed"):
        await Runner.run(agent, state, session=session)

    assert state.pending_input == []
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == 1

    state = await RunState.from_json(agent, state.to_json())
    model.enqueue([get_text_message("Recovered")])
    result = await Runner.run(agent, state, session=session)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_pending_input_session_append_recovers_exactly_once(
    failing_streamed: bool,
    round_trip: bool,
    failure: Literal["before", "after"],
) -> None:
    session = _PendingInputWriteFailureSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    guardrail_calls = 0

    def inspect_pending_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_calls
        guardrail_calls += 1
        return GuardrailFunctionOutput(output_info="accepted", tripwire_triggered=False)

    agent.input_guardrails = [InputGuardrail(guardrail_function=inspect_pending_input)]
    state.add_input("Late input")
    model.enqueue([get_text_message("Recovered")])
    model_calls_before = len(model.calls)
    persisted_count_before = state._current_turn_persisted_item_count
    session.failure = failure

    failed_stream_result = None
    with pytest.raises(RuntimeError, match="pending input Session append failed") as exc_info:
        if failing_streamed:
            failed_stream_result = Runner.run_streamed(
                agent,
                state,
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )
            async for _event in failed_stream_result.stream_events():
                pass
        else:
            await Runner.run(
                agent,
                state,
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )

    assert exc_info.value is session.error
    if failed_stream_result is not None:
        state = failed_stream_result.to_state()
    assert len(model.calls) == model_calls_before
    assert guardrail_calls == 1
    assert len(state._input_guardrail_results) == 1
    assert [_message_text(item) for item in state.pending_input] == ["Late input"]
    pending_write = state._pending_session_write
    assert pending_write is not None
    assert pending_write["persisted_count"] == persisted_count_before
    assert [_message_text(item) for item in pending_write["pending_input"]] == ["Late input"]
    admitted_items = [
        item
        for item in state._generated_items
        if isinstance(item, InputItem) and _message_text(item.raw_item) == "Late input"
    ]
    assert len(admitted_items) == 1
    admitted_input_id = admitted_items[0].input_id
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == (
        0 if failure == "before" else 1
    )
    with pytest.raises(UserError, match="awaiting reconciliation"):
        state.clear_pending_input()

    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    result = await _resume_pending_input_state(
        agent,
        state,
        session,
        streamed=not failing_streamed,
    )

    assert result.final_output == "Recovered"
    assert len(model.calls) == model_calls_before + 1
    assert guardrail_calls == 1
    assert len(state._input_guardrail_results) == 1
    assert state.pending_input == []
    assert state._pending_session_write is None
    assert [_message_text(item) for item in await session.get_items()].count("Late input") == 1
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert [_message_text(item) for item in result.to_input_list()].count("Late input") == 1
    restored_admission_ids = [
        item.input_id
        for item in state._generated_items
        if isinstance(item, InputItem) and _message_text(item.raw_item) == "Late input"
    ]
    assert restored_admission_ids == [admitted_input_id]


@pytest.mark.asyncio
async def test_pending_input_session_checkpoint_requires_schema_1_18() -> None:
    session = _PendingInputWriteFailureSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input("Late input")
    model.enqueue([get_text_message("Recovered")])
    session.failure = "before"

    with pytest.raises(RuntimeError, match="pending input Session append failed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )

    payload = state.to_json()
    assert payload["$schemaVersion"] == CURRENT_SCHEMA_VERSION == "1.18"
    assert payload["pending_session_write"]["pending_input"] == payload["pending_input"]

    restored = await RunState.from_json(agent, payload)
    assert restored._schema_version == "1.18"
    assert restored._pending_session_write is not None
    assert restored._pending_session_write["pending_input"] == restored.pending_input

    disguised_payload = copy.deepcopy(payload)
    disguised_payload["$schemaVersion"] = "1.17"
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, disguised_payload)

    legacy_payload = copy.deepcopy(disguised_payload)
    del legacy_payload["pending_session_write"]["pending_input"]
    legacy_restored = await RunState.from_json(agent, legacy_payload)
    assert legacy_restored._schema_version == "1.17"
    assert legacy_restored._pending_session_write == legacy_payload["pending_session_write"]

    corrupted_batch = copy.deepcopy(payload)
    corrupted_batch["pending_session_write"]["items"][0]["content"] = "Wrong input"
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, corrupted_batch)

    missing_admission = copy.deepcopy(payload)
    missing_admission["generated_items"].pop()
    missing_admission["session_items"].pop()
    missing_admission["generated_session_item_indexes"].pop()
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, missing_admission)

    required_id_corruption = copy.deepcopy(payload)
    owned_call = {
        "type": "file_search_call",
        "id": "file-search-correct",
        "queries": ["checkpoint integrity"],
        "status": "completed",
    }
    required_id_corruption["pending_input"] = [copy.deepcopy(owned_call)]
    required_id_corruption["pending_session_write"]["pending_input"] = [copy.deepcopy(owned_call)]
    required_id_corruption["pending_session_write"]["items"] = [
        {**owned_call, "id": "file-search-wrong"}
    ]
    required_id_corruption["generated_items"][-1]["raw_item"] = copy.deepcopy(owned_call)
    required_id_corruption["session_items"][-1]["raw_item"] = copy.deepcopy(owned_call)
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, required_id_corruption)


@pytest.mark.asyncio
async def test_pending_input_reconciliation_rejects_corrupted_live_batch() -> None:
    session = _PendingInputWriteFailureSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input("Late input")
    model.enqueue([get_text_message("Recovered")])
    session.failure = "before"

    with pytest.raises(RuntimeError, match="pending input Session append failed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )

    pending_write = state._pending_session_write
    assert pending_write is not None
    cast(dict[str, Any], pending_write["items"][0])["content"] = "Wrong input"
    model_calls_before = len(model.calls)

    with pytest.raises(UserError, match="staged input batch changed"):
        await _resume_pending_input_state(agent, state, session, streamed=False)

    assert state._pending_session_write is pending_write
    assert [_message_text(item) for item in state.pending_input] == ["Late input"]
    assert [_message_text(item) for item in await session.get_items()].count("Wrong input") == 0
    assert len(model.calls) == model_calls_before


@pytest.mark.asyncio
async def test_conversations_checkpoint_rejects_corrupted_required_item_id() -> None:
    agent = Agent(name="checkpoint-agent")
    owned_call = cast(
        TResponseInputItem,
        {
            "type": "file_search_call",
            "id": "file-search-correct",
            "queries": ["checkpoint integrity"],
            "status": "completed",
        },
    )
    admission = InputItem(agent=agent, raw_item=owned_call)
    state = RunState(
        context=RunContextWrapper(context={}),
        original_input="original input",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    state._pending_input = [copy.deepcopy(owned_call)]
    state._generated_items = [admission]
    state._session_items = [admission]
    state._pending_session_write = {
        "session_id": "test",
        "items": [cast(TResponseInputItem, {**owned_call, "id": "file-search-wrong"})],
        "before": None,
        "persisted_count": 0,
        "pending_input": [copy.deepcopy(owned_call)],
    }
    session = _CheckpointOpenAIConversationsSession()

    with pytest.raises(UserError, match="staged input batch changed"):
        await resume_pending_session_write(state, session)

    assert session.items == []
    assert state._pending_session_write is not None
    assert state.pending_input == [owned_call]


@pytest.mark.asyncio
async def test_conversations_admits_unpersistable_pending_input_without_append() -> None:
    session = _CheckpointOpenAIConversationsSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    session_items_before = list(session.items)
    reasoning_input = cast(TResponseInputItem, {"type": "reasoning", "summary": []})
    state.add_input([reasoning_input])
    model.enqueue([get_text_message("Recovered")])

    result = await Runner.run(
        agent,
        state,
        session=session,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Recovered"
    assert state.pending_input == []
    assert state._pending_session_write is None
    assert not any(_item_type(item) == "reasoning" for item in session.items)
    assert session.items[: len(session_items_before)] == session_items_before
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert sum(_item_type(item) == "reasoning" for item in model_input) == 1
    assert sum(_item_type(item) == "reasoning" for item in result.to_input_list()) == 1
    admitted_items = [item for item in state._generated_items if isinstance(item, InputItem)]
    assert [item.raw_item for item in admitted_items] == [reasoning_input]
    assert [item for item in state._session_items if isinstance(item, InputItem)] == admitted_items


@pytest.mark.asyncio
async def test_conversations_initializes_lazy_session_before_pending_checkpoint() -> None:
    model, agent, state, _calls = await _make_after_turn_state()
    state.add_input("Late input")
    model.enqueue([get_text_message("Recovered")])
    session = _CheckpointOpenAIConversationsSession(session_id=None)

    result = await Runner.run(
        agent,
        state,
        session=session,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Recovered"
    assert session.initializations == 1
    assert session.session_id == "lazy-test"
    assert [_message_text(item) for item in session.items].count("Late input") == 1
    assert state.pending_input == []
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_streamed", "round_trip"),
    [(False, True), (True, False)],
    ids=["run-json-to-stream", "stream-live-to-run"],
)
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
@pytest.mark.parametrize(
    ("role", "pending_value", "text"),
    [
        ("user", "Normalized user input", "Normalized user input"),
        (
            "assistant",
            [{"role": "assistant", "content": "Normalized assistant input"}],
            "Normalized assistant input",
        ),
    ],
)
async def test_conversations_normalized_pending_append_recovers_exactly_once(
    failing_streamed: bool,
    round_trip: bool,
    failure: Literal["before", "after"],
    role: Literal["user", "assistant"],
    pending_value: str | list[TResponseInputItem],
    text: str,
) -> None:
    session = _NormalizingOpenAIConversationsSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    guardrail_calls = 0

    def inspect_pending_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_calls
        guardrail_calls += 1
        return GuardrailFunctionOutput(output_info="accepted", tripwire_triggered=False)

    agent.input_guardrails = [InputGuardrail(guardrail_function=inspect_pending_input)]
    state.add_input(copy.deepcopy(pending_value))
    model.enqueue([get_text_message("Recovered")])
    model_calls_before = len(model.calls)
    session.failure = failure

    failed_stream_result = None
    with pytest.raises(RuntimeError, match="normalized Conversations append failed") as exc_info:
        if failing_streamed:
            failed_stream_result = Runner.run_streamed(
                agent,
                state,
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )
            async for _event in failed_stream_result.stream_events():
                pass
        else:
            await Runner.run(
                agent,
                state,
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )

    assert exc_info.value is session.error
    if failed_stream_result is not None:
        state = failed_stream_result.to_state()
    extractor = _assistant_message_text if role == "assistant" else _message_text
    assert [extractor(item) for item in session.items].count(text) == (
        0 if failure == "before" else 1
    )
    assert len(model.calls) == model_calls_before
    assert guardrail_calls == 1
    assert state._pending_session_write is not None

    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    result = await _resume_pending_input_state(
        agent,
        state,
        session,
        streamed=not failing_streamed,
    )

    assert result.final_output == "Recovered"
    assert len(model.calls) == model_calls_before + 1
    assert guardrail_calls == 1
    assert state.pending_input == []
    assert state._pending_session_write is None
    assert [extractor(item) for item in session.items].count(text) == 1
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [extractor(item) for item in model_input].count(text) == 1
    assert [extractor(item) for item in result.to_input_list()].count(text) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["changed-text", "unrelated-tail"])
async def test_conversations_normalized_reconciliation_rejects_history_drift(
    drift: str,
) -> None:
    session = _NormalizingOpenAIConversationsSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input("Drift target")
    model.enqueue([get_text_message("Must not run")])
    model_calls_before = len(model.calls)
    session.failure = "after"

    with pytest.raises(RuntimeError, match="normalized Conversations append failed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )

    if drift == "changed-text":
        target = next(item for item in session.items if _message_text(item) == "Drift target")
        assert isinstance(target, dict)
        content = target.get("content")
        assert isinstance(content, list) and isinstance(content[0], dict)
        content[0]["text"] = "Changed by another writer"
    else:
        session.items.append(
            cast(
                TResponseInputItem,
                {
                    "id": "msg_unrelated",
                    "type": "message",
                    "role": "user",
                    "status": "completed",
                    "content": [{"type": "input_text", "text": "Another writer"}],
                },
            )
        )
    history_before_retry = copy.deepcopy(session.items)
    state = await RunState.from_json(agent, state.to_json())

    with pytest.raises(UserError, match="history changed or is ambiguous"):
        await _resume_pending_input_state(agent, state, session, streamed=True)

    assert session.items == history_before_retry
    assert len(model.calls) == model_calls_before
    assert state._pending_session_write is not None
    assert [_message_text(item) for item in state.pending_input] == ["Drift target"]


@pytest.mark.asyncio
async def test_conversations_legacy_checkpoint_reconciles_normalized_tail() -> None:
    session = _NormalizingOpenAIConversationsSession()
    existing = cast(TResponseInputItem, {"role": "user", "content": "before"})
    pending = cast(TResponseInputItem, {"role": "user", "content": "after"})
    await session.add_items([copy.deepcopy(existing)])
    agent = Agent(name="legacy-checkpoint-agent")
    state = RunState(
        context=RunContextWrapper(context={}),
        original_input="original input",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": session.session_id,
        "items": [copy.deepcopy(pending)],
        "before": None,
        "persisted_count": 0,
    }
    session.failure = "after"

    with pytest.raises(RuntimeError, match="normalized Conversations append failed"):
        await resume_pending_session_write(state, session)

    assert [_message_text(item) for item in session.items] == ["before", "after"]
    payload = state.to_json()
    payload["$schemaVersion"] = "1.17"
    restored = await RunState.from_json(agent, payload)

    await resume_pending_session_write(restored, session)

    assert restored._pending_session_write is None
    assert [_message_text(item) for item in session.items] == ["before", "after"]


@pytest.mark.asyncio
async def test_conversations_function_call_response_defaults_reconcile_lost_ack() -> None:
    session = _NormalizingOpenAIConversationsSession()
    function_call = cast(
        TResponseInputItem,
        {
            "type": "function_call",
            "call_id": "call_normalized",
            "name": "lookup",
            "arguments": "{}",
        },
    )
    agent = Agent(name="function-call-checkpoint-agent")
    state = RunState(
        context=RunContextWrapper(context={}),
        original_input="original input",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": session.session_id,
        "items": [copy.deepcopy(function_call)],
        "before": None,
        "persisted_count": 0,
    }
    session.failure = "after"

    with pytest.raises(RuntimeError, match="normalized Conversations append failed"):
        await resume_pending_session_write(state, session)

    stored_call = next(item for item in session.items if _item_type(item) == "function_call")
    assert isinstance(stored_call, dict)
    assert stored_call["status"] == "completed"
    assert stored_call["created_by"] == "server"
    restored = await RunState.from_json(agent, state.to_json())

    await resume_pending_session_write(restored, session)

    assert restored._pending_session_write is None
    assert sum(_item_type(item) == "function_call" for item in session.items) == 1


@pytest.mark.asyncio
async def test_conversations_complete_duplicate_tail_recognizes_lost_ack() -> None:
    session = _NormalizingOpenAIConversationsSession()
    duplicate = cast(TResponseInputItem, {"role": "user", "content": "same"})
    await session.add_items([copy.deepcopy(duplicate)])
    agent = Agent(name="complete-checkpoint-agent")
    state = RunState(
        context=RunContextWrapper(context={}),
        original_input="original input",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": session.session_id,
        "items": [copy.deepcopy(duplicate)],
        "before": None,
        "persisted_count": 0,
    }
    session.failure = "after"

    with pytest.raises(RuntimeError, match="normalized Conversations append failed"):
        await resume_pending_session_write(state, session)

    assert [_message_text(item) for item in session.items].count("same") == 2
    restored = await RunState.from_json(agent, state.to_json())

    await resume_pending_session_write(restored, session)

    assert [_message_text(item) for item in session.items].count("same") == 2
    assert restored._pending_session_write is None


@pytest.mark.asyncio
async def test_conversations_saturated_duplicate_tail_remains_fail_closed() -> None:
    session = _NormalizingOpenAIConversationsSession()
    duplicate = cast(TResponseInputItem, {"role": "user", "content": "same"})
    for _ in range(3):
        await session.add_items([copy.deepcopy(duplicate)])
    agent = Agent(name="saturated-checkpoint-agent")
    state = RunState(
        context=RunContextWrapper(context={}),
        original_input="original input",
        starting_agent=agent,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": session.session_id,
        "items": [copy.deepcopy(duplicate)],
        "before": None,
        "persisted_count": 0,
    }
    session.failure = "before"

    with pytest.raises(RuntimeError, match="normalized Conversations append failed"):
        await resume_pending_session_write(state, session)

    restored = await RunState.from_json(agent, state.to_json())
    history_before_retry = copy.deepcopy(session.items)
    with pytest.raises(UserError, match="history changed or is ambiguous"):
        await resume_pending_session_write(restored, session)

    assert session.items == history_before_retry
    assert [_message_text(item) for item in session.items].count("same") == 3
    assert restored._pending_session_write is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_pending_input_assistant_message_round_trip_recovers_exactly_once(
    failure: Literal["before", "after"],
) -> None:
    session = _PendingInputWriteFailureSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    state.add_input([{"role": "assistant", "content": "Late assistant"}])
    model.enqueue([get_text_message("Recovered")])
    session.failure = failure

    with pytest.raises(RuntimeError, match="pending input Session append failed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )

    state = await RunState.from_json(agent, state.to_json())
    result = await _resume_pending_input_state(agent, state, session, streamed=True)

    assert result.final_output == "Recovered"
    assert state.pending_input == []
    assert state._pending_session_write is None
    assert [_assistant_message_text(item) for item in await session.get_items()].count(
        "Late assistant"
    ) == 1
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_assistant_message_text(item) for item in model_input].count("Late assistant") == 1
    assert [_assistant_message_text(item) for item in result.to_input_list()].count(
        "Late assistant"
    ) == 1


@pytest.mark.asyncio
async def test_pending_input_reconciliation_consumes_only_its_owned_prefix() -> None:
    session = _PendingInputWriteFailureSession()
    model, agent, state, _calls = await _make_after_turn_state(session=session)
    guarded_batches: list[list[str | None]] = []

    def record_guarded_input(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        guarded_input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        assert isinstance(guarded_input, list)
        guarded_batches.append([_message_text(item) for item in guarded_input])
        return GuardrailFunctionOutput(output_info="accepted", tripwire_triggered=False)

    agent.input_guardrails = [InputGuardrail(guardrail_function=record_guarded_input)]
    state.add_input("First late input")
    model.enqueue([get_text_message("Recovered")])
    session.failure = "after"
    failed_result = Runner.run_streamed(
        agent,
        state,
        session=session,
        run_config=RunConfig(tracing_disabled=True),
    )

    with pytest.raises(RuntimeError, match="pending input Session append failed"):
        async for _event in failed_result.stream_events():
            pass

    state = failed_result.to_state()
    state.add_input("Second late input")
    state = await RunState.from_json(agent, state.to_json())
    result = await _resume_pending_input_state(agent, state, session, streamed=False)

    assert result.final_output == "Recovered"
    assert guarded_batches == [["First late input"], ["Second late input"]]
    session_texts = [_message_text(item) for item in await session.get_items()]
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    model_texts = [_message_text(item) for item in model_input]
    for text in ("First late input", "Second late input"):
        assert session_texts.count(text) == 1
        assert model_texts.count(text) == 1
    assert state.pending_input == []
    assert state._pending_session_write is None


@pytest.mark.asyncio
async def test_failed_server_managed_request_keeps_pending_input_for_retry() -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.enqueue(RuntimeError("model failed"))

    with pytest.raises(RuntimeError, match="model failed"):
        await Runner.run(agent, state)

    assert _message_text(state.pending_input[0]) == "Late input"
    state = await RunState.from_json(agent, state.to_json())
    model.enqueue([get_text_message("Recovered")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Recovered"
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert state.pending_input == []


@pytest.mark.asyncio
async def test_server_filter_omission_remains_pending_for_later_nonstream_turn() -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.extend(
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
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1
    assert state.pending_input == []


@pytest.mark.asyncio
async def test_server_filter_omission_survives_streamed_state_round_trip() -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.enqueue(
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

    model.enqueue([get_text_message("Done")])
    result = await Runner.run(agent, state)
    assert result.final_output == "Done"
    assert calls == ["Paris", "Rome"]
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in model_input].count("Late input") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_server_filter_reconstructed_pending_rewrite_is_rejected(streamed: bool) -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.enqueue([get_text_message("Done")])

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

    queued_outputs = model.remaining_steps
    run_config = RunConfig(call_model_input_filter=reconstruct_pending)
    if streamed:
        failed = Runner.run_streamed(agent, state, run_config=run_config)
        with pytest.raises(UserError, match="cannot safely associate"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(UserError, match="cannot safely associate"):
            await Runner.run(agent, state, run_config=run_config)

    assert model.remaining_steps == queued_outputs
    assert [_message_text(item) for item in state.pending_input] == ["Late input"]


@pytest.mark.asyncio
async def test_server_filter_in_place_pending_rewrite_preserves_occurrence() -> None:
    model, agent, state, _calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.enqueue([get_text_message("Done")])

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
    model_input = cast(list[TResponseInputItem], model.calls[-1].input)
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
    model.enqueue([get_text_message("Accepted")])

    if streamed_failure:
        failed = Runner.run_streamed(agent, state, hooks=FailAfterResponse())
        with pytest.raises(RuntimeError, match="after response"):
            async for _event in failed.stream_events():
                pass
    else:
        with pytest.raises(RuntimeError, match="after response"):
            await Runner.run(agent, state, hooks=FailAfterResponse())

    accepted_model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in accepted_model_input].count("Late input") == 1
    assert state.pending_input == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted
    assert state._current_step.llm_end_hooks_started
    assert agent_hooks.call_count == 1
    state = await RunState.from_json(agent, state.to_json())
    queued_outputs = model.remaining_steps

    recovered = await Runner.run(agent, state)
    assert recovered.final_output == "Accepted"
    assert agent_hooks.call_count == 1
    assert model.remaining_steps == queued_outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed_failure", [False, True])
async def test_server_acceptance_commits_before_invocation_validation_failure(
    streamed_failure: bool,
) -> None:
    model, agent, state, calls = await _make_after_turn_state(auto_previous_response_id=True)
    state.add_input("Late input")
    model.enqueue(
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

    accepted_model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in accepted_model_input].count("Late input") == 1
    assert state.pending_input == []
    assert isinstance(state._current_step, NextStepInterruption)
    assert state._current_step.response_accepted
    assert state._last_processed_response is None
    assert calls == ["Paris"]

    state = await RunState.from_json(agent, state.to_json())
    queued_outputs = model.remaining_steps
    with pytest.raises(UserError, match="accepted model response could not be processed"):
        await Runner.run(agent, state)
    assert model.remaining_steps == queued_outputs
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
    output = [
        ResponseComputerToolCall(
            id="computer-item",
            type="computer_call",
            action=ActionScreenshot(type="screenshot"),
            call_id="computer-call",
            pending_safety_checks=[],
            status="completed",
        )
    ]
    model.enqueue(get_exact_output_stream_step(output) if streamed_failure else output)
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
    model.extend(
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
    retry_model_input = cast(list[TResponseInputItem], model.calls[-1].input)
    assert [_message_text(item) for item in retry_model_input].count("Late input") == 0


@pytest.mark.asyncio
async def test_terminal_state_rejects_pending_input_without_mutation() -> None:
    model = ScriptedModel(steps=[[get_text_message("Done")]])
    agent = Agent(name="assistant", model=model)
    result = await Runner.run(agent, "Initial request")
    state = result.to_state()
    before = state.to_json()

    with pytest.raises(UserError, match="terminal RunState"):
        state.add_input("Too late")

    assert state.to_json() == before
