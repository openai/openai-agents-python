import asyncio
import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

import agents.run as run_module
from agents import Agent, ModelSettings, Runner, function_tool
from agents.agent import ToolsToFinalOutputResult
from agents.agent_output import AgentOutputSchema
from agents.exceptions import UserError
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail
from agents.items import (
    MessageOutputItem,
    ModelResponse,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from agents.lifecycle import RunHooks
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal import run_loop, turn_resolution
from agents.run_internal.agent_bindings import bind_public_agent
from agents.run_internal.run_loop import (
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    SingleStepResult,
)
from agents.run_state import RunState
from agents.testing import ScriptedModel
from agents.usage import Usage
from tests.test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message
from tests.utils.hitl import (
    make_agent,
    make_context_wrapper,
    make_model_and_agent,
    queue_function_call_and_text,
)
from tests.utils.simple_session import SimpleListSession


class _FailNextAtomicAddSession(SimpleListSession):
    """Fail selected atomic appends before mutating the in-memory history."""

    def __init__(self) -> None:
        super().__init__(session_id="fail-next-atomic-add")
        self.fail_next_add = False

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if self.fail_next_add:
            self.fail_next_add = False
            raise RuntimeError("injected atomic Session.add_items failure")
        await super().add_items(items)


class _FailAfterAppendCompactionSession(_FailNextAtomicAddSession):
    """Fail deferred compaction after the resumed output batch was appended."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_deferred_compaction = False
        self.deferred_compaction_calls: list[tuple[str, bool | None]] = []

    async def run_compaction(self, args: Any = None) -> None:
        pass

    async def _defer_compaction(self, response_id: str, *, store: bool | None = None) -> None:
        self.deferred_compaction_calls.append((response_id, store))
        if self.fail_deferred_compaction:
            self.fail_deferred_compaction = False
            raise RuntimeError("injected post-append compaction failure")


async def _run_in_mode(
    mode: str,
    agent: Agent[Any],
    input_value: Any,
    session: SimpleListSession,
    run_config: RunConfig,
) -> Any:
    if mode == "non_streamed":
        return await Runner.run(agent, input_value, session=session, run_config=run_config)
    result = Runner.run_streamed(agent, input_value, session=session, run_config=run_config)
    async for _ in result.stream_events():
        pass
    return result


async def _run_expecting_atomic_failure(
    mode: str,
    agent: Agent[Any],
    state: RunState[Any],
    session: _FailNextAtomicAddSession,
    run_config: RunConfig,
) -> Any | None:
    if mode == "non_streamed":
        with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
            await Runner.run(agent, state, session=session, run_config=run_config)
        return None
    result = Runner.run_streamed(agent, state, session=session, run_config=run_config)
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        async for _ in result.stream_events():
            pass
    return result


def _count_call_items(items: list[TResponseInputItem], item_type: str, call_id: str) -> int:
    return sum(
        isinstance(item, dict) and item.get("type") == item_type and item.get("call_id") == call_id
        for item in items
    )


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_final_output_short_circuit(monkeypatch) -> None:
    agent: Agent[dict[str, str]] = make_agent(model=ScriptedModel())
    context_wrapper = make_context_wrapper()

    async def fake_execute_tool_plan(*_: object, **__: object):
        return [], [], [], [], [], [], [], []

    async def fake_check_for_final_output_from_tools(*_: object, **__: object):
        return ToolsToFinalOutputResult(is_final_output=True, final_output="done")

    async def fake_execute_final_output(
        *,
        original_input,
        new_response,
        pre_step_items,
        new_step_items,
        final_output,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        **__: object,
    ) -> SingleStepResult:
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepFinalOutput(final_output),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    monkeypatch.setattr(
        turn_resolution, "check_for_final_output_from_tools", fake_check_for_final_output_from_tools
    )
    monkeypatch.setattr(turn_resolution, "execute_final_output", fake_execute_final_output)
    monkeypatch.setattr(turn_resolution, "_execute_tool_plan", fake_execute_tool_plan)

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await run_loop.resolve_interrupted_turn(
        bindings=bind_public_agent(agent),
        original_input="input",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=None,
    )

    assert isinstance(result, SingleStepResult)
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "done"


@pytest.mark.asyncio
async def test_resumed_session_persistence_uses_saved_count(monkeypatch) -> None:
    agent = Agent(name="resume-agent")
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="input",
        starting_agent=agent,
        max_turns=1,
    )
    session = SimpleListSession()

    raw_output = {"type": "function_call_output", "call_id": "call-1", "output": "ok"}
    item_1 = ToolCallOutputItem(agent=agent, raw_item=raw_output, output="ok")
    item_2 = ToolCallOutputItem(agent=agent, raw_item=dict(raw_output), output="ok")
    step = SingleStepResult(
        original_input="input",
        model_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        pre_step_items=[],
        new_step_items=[item_1, item_2],
        next_step=NextStepFinalOutput("done"),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
    )

    async def fake_run_single_turn(**_kwargs):
        return step

    monkeypatch.setattr(run_module, "run_single_turn", fake_run_single_turn)

    runner = run_module.AgentRunner()
    await runner.run(agent, state, session=session, run_config=RunConfig())

    assert state._current_turn_persisted_item_count == 1
    assert len(session.saved_items) == 1


@pytest.mark.asyncio
async def test_resumed_run_again_resets_persisted_count(monkeypatch) -> None:
    agent = Agent(name="resume-agent")
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="input",
        starting_agent=agent,
        max_turns=2,
    )
    session = SimpleListSession()

    state._current_step = NextStepInterruption(interruptions=[])
    state._model_responses = [
        ModelResponse(output=[], usage=Usage(), response_id="resp_1"),
    ]
    state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    state._current_turn_persisted_item_count = 1

    async def fake_resolve_interrupted_turn(**_kwargs):
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(output=[], usage=Usage(), response_id="resp_resume"),
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepRunAgain(),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    async def fake_run_single_turn(**_kwargs):
        tool_call = cast(
            ResponseFunctionToolCall,
            get_function_tool_call("test_tool", "{}", call_id="call-1"),
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)
        tool_output_item = ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ok",
            },
            output="ok",
        )
        message_item = MessageOutputItem(
            agent=agent,
            raw_item=cast(ResponseOutputMessage, get_text_message("final")),
        )
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(
                output=[get_text_message("final")],
                usage=Usage(),
                response_id="resp_final",
            ),
            pre_step_items=[],
            new_step_items=[tool_call_item, tool_output_item, message_item],
            next_step=NextStepFinalOutput("done"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(run_module, "resolve_interrupted_turn", fake_resolve_interrupted_turn)
    monkeypatch.setattr(run_module, "run_single_turn", fake_run_single_turn)

    runner = run_module.AgentRunner()
    result = await runner.run(agent, state, session=session, run_config=RunConfig())

    assert result.final_output == "done"
    saved_types = [
        item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        for item in session.saved_items
    ]
    assert "function_call" in saved_types


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["run_again", "handoff"])
async def test_resumed_stream_waits_for_event_consumption_before_continuing(
    monkeypatch: pytest.MonkeyPatch,
    continuation: str,
) -> None:
    agent = Agent(name="resume-agent")
    delegate = Agent(name="delegate", output_type=int)
    state: RunState[dict[str, str]] = RunState(
        context=RunContextWrapper(context={}),
        original_input="input",
        starting_agent=agent,
        max_turns=2,
    )
    state._current_step = NextStepInterruption(interruptions=[])
    state._model_responses = [
        ModelResponse(output=[], usage=Usage(), response_id="resp_1"),
    ]
    state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    tool_output_item = ToolCallOutputItem(
        agent=agent,
        raw_item={
            "type": "function_call_output",
            "call_id": "call-resume",
            "output": "ok",
        },
        output="ok",
    )
    next_step = NextStepHandoff(delegate) if continuation == "handoff" else NextStepRunAgain()
    allow_resume_resolution = asyncio.Event()

    async def fake_resolve_interrupted_turn(**_kwargs: object) -> SingleStepResult:
        await allow_resume_resolution.wait()
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(output=[], usage=Usage(), response_id="resp_resume"),
            pre_step_items=[],
            new_step_items=[tool_output_item],
            next_step=next_step,
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    next_model_turn_started = asyncio.Event()
    allow_model_turn_to_finish = asyncio.Event()

    async def fake_run_single_turn_streamed(*_args: object, **_kwargs: object) -> SingleStepResult:
        next_model_turn_started.set()
        await allow_model_turn_to_finish.wait()
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(output=[], usage=Usage(), response_id="unexpected"),
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepFinalOutput("unexpected"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(run_loop, "resolve_interrupted_turn", fake_resolve_interrupted_turn)
    monkeypatch.setattr(run_loop, "run_single_turn_streamed", fake_run_single_turn_streamed)

    result = Runner.run_streamed(agent, state)
    consumer_active = asyncio.Event()
    consumer_suspended = asyncio.Event()
    release_consumer = asyncio.Event()
    cancel_called = asyncio.Event()

    async def consume_events() -> None:
        async for event in result.stream_events():
            if event.type == "agent_updated_stream_event":
                consumer_active.set()
            if event.type == "run_item_stream_event" and event.name == "tool_output":
                consumer_suspended.set()
                await release_consumer.wait()
                result.cancel(mode="after_turn")
                cancel_called.set()

    consumer_task = asyncio.create_task(consume_events())
    await asyncio.wait_for(consumer_active.wait(), timeout=1)
    allow_resume_resolution.set()
    await asyncio.wait_for(consumer_suspended.wait(), timeout=1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not next_model_turn_started.is_set()

    release_consumer.set()
    await asyncio.wait_for(cancel_called.wait(), timeout=1)
    allow_model_turn_to_finish.set()
    await asyncio.wait_for(consumer_task, timeout=1)

    assert not next_model_turn_started.is_set()
    assert result.final_output is None
    expected_agent = delegate if continuation == "handoff" else agent
    assert result.current_agent is expected_agent
    assert result.last_agent is expected_agent
    assert result.to_state()._current_agent is expected_agent
    if continuation == "handoff":
        assert result._current_agent_output_schema is not None
        assert isinstance(result._current_agent_output_schema, AgentOutputSchema)
        assert result._current_agent_output_schema.output_type is int


@pytest.mark.parametrize(
    ("conversation_id", "previous_response_id", "auto_previous_response_id"),
    [
        ("conv_1", None, False),
        (None, "resp_prev", False),
        (None, None, True),
    ],
)
@pytest.mark.asyncio
async def test_resumed_interruption_passes_server_managed_conversation_flag(
    monkeypatch: pytest.MonkeyPatch,
    conversation_id: str | None,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
) -> None:
    agent = Agent(name="resume-agent")
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="input",
        starting_agent=agent,
        max_turns=1,
        conversation_id=conversation_id,
        previous_response_id=previous_response_id,
        auto_previous_response_id=auto_previous_response_id,
    )

    state._current_step = NextStepInterruption(interruptions=[])
    state._model_responses = [
        ModelResponse(output=[], usage=Usage(), response_id="resp_1"),
    ]
    state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    server_managed_values: list[bool] = []

    async def fake_resolve_interrupted_turn(**kwargs: object) -> SingleStepResult:
        server_managed_values.append(cast(bool, kwargs["server_manages_conversation"]))
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(output=[], usage=Usage(), response_id="resp_resume"),
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepFinalOutput("done"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(run_module, "resolve_interrupted_turn", fake_resolve_interrupted_turn)

    runner = run_module.AgentRunner()
    result = await runner.run(agent, state, run_config=RunConfig())

    assert result.final_output == "done"
    assert server_managed_values == [True]


@pytest.mark.asyncio
async def test_resumed_approval_does_not_duplicate_session_items() -> None:
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

    first = await Runner.run(agent, input="Use test_tool", session=session)
    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(agent, state, session=session)
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


@pytest.mark.parametrize(
    ("pause_mode", "failing_mode", "retry_mode", "round_trip"),
    [
        ("non_streamed", "non_streamed", "non_streamed", False),
        ("non_streamed", "non_streamed", "streamed", True),
        ("non_streamed", "streamed", "non_streamed", True),
        ("non_streamed", "streamed", "streamed", False),
        ("streamed", "non_streamed", "streamed", False),
        ("streamed", "non_streamed", "non_streamed", True),
        ("streamed", "streamed", "non_streamed", False),
        ("streamed", "streamed", "streamed", True),
    ],
)
@pytest.mark.asyncio
async def test_resumed_approval_retries_failed_atomic_session_write_before_model_call(
    pause_mode: str,
    failing_mode: str,
    retry_mode: str,
    round_trip: bool,
) -> None:
    side_effects: list[int] = []

    @function_tool(needs_approval=True)
    async def charge(amount: int) -> str:
        side_effects.append(amount)
        return f"charged:{amount}"

    call_id = "call-charge"
    model = ScriptedModel(
        [
            [get_function_tool_call("charge", json.dumps({"amount": 7}), call_id=call_id)],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[charge])
    session = _FailNextAtomicAddSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await _run_in_mode(pause_mode, agent, "charge 7", session, run_config)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    session.fail_next_add = True
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        await _run_in_mode(failing_mode, agent, state, session, run_config)

    assert side_effects == [7]
    assert len(model.calls) == 1
    assert _count_call_items(await session.get_items(), "function_call", call_id) == 1
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 0

    with pytest.raises(UserError, match="without the original Session"):
        await Runner.run(agent, state, run_config=run_config)
    with pytest.raises(UserError, match="using a different Session"):
        await Runner.run(
            agent,
            state,
            session=SimpleListSession(session_id="different-session"),
            run_config=run_config,
        )
    assert len(model.calls) == 1

    if round_trip:
        serialized_state = state.to_json()
        assert serialized_state["$schemaVersion"] == "1.17"
        assert serialized_state["pending_session_write"]["session_id"] == session.session_id
        assert serialized_state["pending_session_write"]["items"]
        malformed_state = json.loads(json.dumps(serialized_state))
        malformed_state["pending_session_write"]["items"] = [{"type": "bogus"}]
        with pytest.raises(UserError, match="pending_session_write items must all be valid"):
            await RunState.from_json(agent, malformed_state)
        empty_pending_state = json.loads(json.dumps(serialized_state))
        empty_pending_state["pending_session_write"]["items"] = []
        with pytest.raises(UserError, match="pending_session_write items must not be empty"):
            await RunState.from_json(agent, empty_pending_state)
        legacy_labeled_state = json.loads(json.dumps(serialized_state))
        legacy_labeled_state["$schemaVersion"] = "1.16"
        with pytest.raises(UserError, match="pending_session_write requires schema version 1.17"):
            await RunState.from_json(agent, legacy_labeled_state)
        for invalid_count in ("0", True, -1):
            invalid_count_state = json.loads(json.dumps(serialized_state))
            invalid_count_state["current_turn_persisted_item_count"] = invalid_count
            with pytest.raises(UserError, match="must be a non-negative integer"):
                await RunState.from_json(agent, invalid_count_state)
        missing_count_state = json.loads(json.dumps(serialized_state))
        missing_count_state.pop("current_turn_persisted_item_count")
        missing_count_restored = await RunState.from_json(agent, missing_count_state)
        assert missing_count_restored._current_turn_persisted_item_count == 0
        state = await RunState.from_json(agent, serialized_state)

    session.fail_next_add = True
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        await _run_in_mode(retry_mode, agent, state, session, run_config)

    assert side_effects == [7]
    assert len(model.calls) == 1

    resumed = await _run_in_mode(retry_mode, agent, state, session, run_config)

    assert resumed.final_output == "done"
    assert side_effects == [7]
    assert len(model.calls) == 2
    saved_items = await session.get_items()
    replay_items = resumed.to_input_list()
    assert _count_call_items(saved_items, "function_call", call_id) == 1
    assert _count_call_items(saved_items, "function_call_output", call_id) == 1
    assert _count_call_items(replay_items, "function_call", call_id) == 1
    assert _count_call_items(replay_items, "function_call_output", call_id) == 1


@pytest.mark.parametrize(
    ("failing_mode", "retry_mode", "round_trip"),
    [
        ("non_streamed", "non_streamed", False),
        ("non_streamed", "streamed", True),
        ("streamed", "non_streamed", True),
        ("streamed", "streamed", False),
    ],
)
@pytest.mark.asyncio
async def test_resumed_approval_and_handoff_retries_session_write_before_target_model(
    failing_mode: str,
    retry_mode: str,
    round_trip: bool,
) -> None:
    side_effects: list[str] = []

    @function_tool(needs_approval=True)
    async def approved_tool() -> str:
        side_effects.append("ran")
        return "approved"

    source_model = ScriptedModel()
    target_model = ScriptedModel([[get_text_message("done")]])
    target = Agent(
        name="target",
        model=target_model,
        model_settings=ModelSettings(store=True),
    )
    source = Agent(
        name="source",
        model=source_model,
        model_settings=ModelSettings(store=False),
        tools=[approved_tool],
        handoffs=[target],
    )
    approved_call_id = "call-approved-before-handoff"
    handoff_call_id = "call-handoff-after-approval"
    source_model.enqueue(
        [
            get_function_tool_call("approved_tool", "{}", call_id=approved_call_id),
            get_handoff_tool_call(target, call_id=handoff_call_id),
        ]
    )
    session = _FailAfterAppendCompactionSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(
        source, "approve and hand off", session=session, run_config=run_config
    )
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    session.fail_next_add = True
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        await _run_in_mode(failing_mode, source, state, session, run_config)

    assert side_effects == ["ran"]
    assert len(source_model.calls) == 1
    assert len(target_model.calls) == 0
    assert isinstance(state._current_step, NextStepRunAgain)
    assert state._current_agent is target
    assert (
        _count_call_items(await session.get_items(), "function_call_output", approved_call_id) == 0
    )
    assert (
        _count_call_items(await session.get_items(), "function_call_output", handoff_call_id) == 0
    )

    if round_trip:
        serialized_state = state.to_json()
        assert serialized_state["pending_session_write"] is not None
        assert serialized_state["pending_session_write"]["store"] is False
        invalid_store_state = json.loads(json.dumps(serialized_state))
        invalid_store_state["pending_session_write"]["store"] = "false"
        with pytest.raises(UserError, match="store must be a boolean or null"):
            await RunState.from_json(source, invalid_store_state)
        state = await RunState.from_json(source, serialized_state)
        assert state._current_agent is target

    session.fail_next_add = True
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        await _run_in_mode(retry_mode, source, state, session, run_config)

    assert side_effects == ["ran"]
    assert len(source_model.calls) == 1
    assert len(target_model.calls) == 0

    resumed = await _run_in_mode(retry_mode, source, state, session, run_config)

    assert resumed.final_output == "done"
    assert resumed.last_agent is target
    assert side_effects == ["ran"]
    assert len(source_model.calls) == 1
    assert len(target_model.calls) == 1
    saved_items = await session.get_items()
    assert _count_call_items(saved_items, "function_call_output", approved_call_id) == 1
    assert _count_call_items(saved_items, "function_call_output", handoff_call_id) == 1
    assert len(session.deferred_compaction_calls) == 1
    assert session.deferred_compaction_calls[0][1] is False


@pytest.mark.parametrize(
    ("failing_mode", "retry_mode", "round_trip"),
    [
        ("non_streamed", "non_streamed", True),
        ("non_streamed", "streamed", False),
        ("streamed", "non_streamed", False),
        ("streamed", "streamed", True),
    ],
)
@pytest.mark.asyncio
async def test_terminal_approval_retries_session_write_without_another_model_call(
    failing_mode: str,
    retry_mode: str,
    round_trip: bool,
) -> None:
    side_effects: list[str] = []

    @function_tool(needs_approval=True)
    async def terminal_tool() -> str:
        side_effects.append("ran")
        return "terminal-output"

    call_id = "call-terminal-after-approval"
    model = ScriptedModel([[get_function_tool_call("terminal_tool", "{}", call_id=call_id)]])
    agent = Agent(
        name="terminal-agent",
        model=model,
        tools=[terminal_tool],
        tool_use_behavior="stop_on_first_tool",
    )
    session = _FailNextAtomicAddSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(agent, "run terminal tool", session=session, run_config=run_config)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    session.fail_next_add = True
    failed_result = await _run_expecting_atomic_failure(
        failing_mode,
        agent,
        state,
        session,
        run_config,
    )

    assert side_effects == ["ran"]
    assert len(model.calls) == 1
    assert isinstance(state._current_step, NextStepFinalOutput)
    assert state._pending_session_items

    if failed_result is not None:
        state = failed_result.to_state()
        assert isinstance(state._current_step, NextStepFinalOutput)
        assert state._pending_session_items

    if round_trip:
        payload = state.to_json()
        assert payload["current_step"]["type"] == "next_step_final_output"

        legacy_final_payload = json.loads(json.dumps(payload))
        legacy_final_payload["$schemaVersion"] = "1.16"
        legacy_final_payload["pending_session_write"] = None
        with pytest.raises(UserError, match="pending final output requires schema version 1.17"):
            await RunState.from_json(agent, legacy_final_payload)

        malformed_final_payload = json.loads(json.dumps(payload))
        malformed_final_payload["current_step"]["data"] = []
        with pytest.raises(UserError, match="pending final output data must contain output"):
            await RunState.from_json(agent, malformed_final_payload)

        malformed_step_payload = json.loads(json.dumps(payload))
        malformed_step_payload["current_step"] = []
        with pytest.raises(UserError, match="current_step must be an object or null"):
            await RunState.from_json(agent, malformed_step_payload)

        state = await RunState.from_json(agent, payload)

    resumed = await _run_in_mode(retry_mode, agent, state, session, run_config)

    assert resumed.final_output == "terminal-output"
    assert side_effects == ["ran"]
    assert len(model.calls) == 1
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 1


@pytest.mark.asyncio
async def test_non_streamed_terminal_guardrail_failure_retries_session_write_before_model() -> None:
    side_effects: list[str] = []

    @function_tool(needs_approval=True)
    async def terminal_tool() -> str:
        side_effects.append("ran")
        return "terminal-output"

    def block_output(
        _context: RunContextWrapper[Any],
        _agent: Agent[Any],
        _output: Any,
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    call_id = "call-terminal-guardrail-failure"
    model = ScriptedModel([[get_function_tool_call("terminal_tool", "{}", call_id=call_id)]])
    agent = Agent(
        name="terminal-guardrail-agent",
        model=model,
        tools=[terminal_tool],
        tool_use_behavior="stop_on_first_tool",
        output_guardrails=[OutputGuardrail(guardrail_function=block_output)],
    )
    session = _FailNextAtomicAddSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(agent, "run terminal tool", session=session, run_config=run_config)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    session.fail_next_add = True
    with pytest.raises(UserError):
        await Runner.run(agent, state, session=session, run_config=run_config)
    assert state._pending_session_items
    assert len(model.calls) == 1
    assert side_effects == ["ran"]

    session.fail_next_add = True
    with pytest.raises(RuntimeError, match="injected atomic Session.add_items failure"):
        await Runner.run(agent, state, session=session, run_config=run_config)
    assert state._pending_session_items
    assert len(model.calls) == 1
    assert side_effects == ["ran"]

    agent.output_guardrails = []
    model.enqueue([get_text_message("continued")])
    resumed = await Runner.run(agent, state, session=session, run_config=run_config)

    assert resumed.final_output == "continued"
    assert len(model.calls) == 2
    assert side_effects == ["ran"]
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 1


@pytest.mark.parametrize("continuation", ["run_again", "handoff"])
@pytest.mark.asyncio
async def test_failed_stream_to_state_preserves_pending_session_barrier(
    continuation: str,
) -> None:
    side_effects: list[str] = []

    @function_tool(needs_approval=True)
    async def approved_tool() -> str:
        side_effects.append("ran")
        return "approved"

    source_model = ScriptedModel()
    target_model = ScriptedModel([[get_text_message("target-done")]])
    target = Agent(name="target", model=target_model)
    source = Agent(
        name="source",
        model=source_model,
        tools=[approved_tool],
        handoffs=[target] if continuation == "handoff" else [],
    )
    call_id = f"call-stream-state-{continuation}"
    first_response = [get_function_tool_call("approved_tool", "{}", call_id=call_id)]
    if continuation == "handoff":
        first_response.append(get_handoff_tool_call(target, call_id="call-stream-handoff"))
        source_model.enqueue(first_response)
        expected_output = "target-done"
    else:
        source_model = ScriptedModel([first_response, [get_text_message("source-done")]])
        source.model = source_model
        expected_output = "source-done"

    session = _FailNextAtomicAddSession()
    run_config = RunConfig(tracing_disabled=True)
    paused = await Runner.run(source, "approve", session=session, run_config=run_config)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    session.fail_next_add = True

    failed_result = await _run_expecting_atomic_failure(
        "streamed",
        source,
        state,
        session,
        run_config,
    )
    assert failed_result is not None
    copied = failed_result.to_state()

    assert copied._pending_session_items
    if continuation == "handoff":
        assert copied._current_agent is target

    resumed = await Runner.run(source, copied, session=session, run_config=run_config)
    assert resumed.final_output == expected_output
    assert side_effects == ["ran"]
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 1


@pytest.mark.asyncio
async def test_resumed_approval_does_not_retry_after_post_append_compaction_failure() -> None:
    side_effects: list[int] = []

    @function_tool(needs_approval=True)
    async def charge(amount: int) -> str:
        side_effects.append(amount)
        return f"charged:{amount}"

    call_id = "call-charge-post-append"
    model = ScriptedModel(
        [
            [get_function_tool_call("charge", json.dumps({"amount": 9}), call_id=call_id)],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[charge])
    session = _FailAfterAppendCompactionSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(agent, "charge 9", session=session, run_config=run_config)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    session.fail_deferred_compaction = True
    with pytest.raises(RuntimeError, match="injected post-append compaction failure"):
        await Runner.run(agent, state, session=session, run_config=run_config)

    assert side_effects == [9]
    assert len(model.calls) == 1
    assert state.to_json()["pending_session_write"] is None
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 1

    resumed = await Runner.run(agent, state, session=session, run_config=run_config)

    assert resumed.final_output == "done"
    assert side_effects == [9]
    assert len(model.calls) == 2
    assert _count_call_items(await session.get_items(), "function_call_output", call_id) == 1


@pytest.mark.asyncio
async def test_failed_stream_to_state_preserves_handoff_after_post_append_failure() -> None:
    side_effects: list[str] = []

    @function_tool(needs_approval=True)
    async def approved_tool() -> str:
        side_effects.append("ran")
        return "approved"

    source_model = ScriptedModel()
    target_model = ScriptedModel([[get_text_message("target-done")]])
    target = Agent(name="target", model=target_model)
    source = Agent(
        name="source",
        model=source_model,
        tools=[approved_tool],
        handoffs=[target],
    )
    approved_call_id = "call-post-append-approved"
    source_model.enqueue(
        [
            get_function_tool_call("approved_tool", "{}", call_id=approved_call_id),
            get_handoff_tool_call(target, call_id="call-post-append-handoff"),
        ]
    )
    session = _FailAfterAppendCompactionSession()
    run_config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(
        source, "approve and hand off", session=session, run_config=run_config
    )
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    session.fail_deferred_compaction = True

    failed_result = Runner.run_streamed(
        source,
        state,
        session=session,
        run_config=run_config,
    )
    with pytest.raises(RuntimeError, match="injected post-append compaction failure"):
        async for _ in failed_result.stream_events():
            pass

    assert state._pending_session_items == []
    assert state._current_agent is target
    assert isinstance(state._current_step, NextStepRunAgain)

    copied = failed_result.to_state()
    assert copied._pending_session_items == []
    assert copied._current_agent is target
    assert isinstance(copied._current_step, NextStepRunAgain)

    resumed = await Runner.run(source, copied, session=session, run_config=run_config)

    assert resumed.final_output == "target-done"
    assert resumed.last_agent is target
    assert side_effects == ["ran"]
    assert len(source_model.calls) == 1
    assert len(target_model.calls) == 1
    assert (
        _count_call_items(await session.get_items(), "function_call_output", approved_call_id) == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_version", "expect_execution"),
    [("1.6", True), ("1.7", False)],
)
async def test_resolve_interrupted_turn_only_uses_name_fallback_for_legacy_approval_agents(
    schema_version: str,
    expect_execution: bool,
) -> None:
    calls: list[str] = []

    @function_tool(name_override="needs_ok", needs_approval=True)
    async def needs_ok(text: str) -> str:
        calls.append(text)
        return text

    base_duplicate = Agent(name="duplicate", instructions="alpha", tools=[needs_ok])
    resumed_duplicate = Agent(name="duplicate", instructions="zeta", tools=[needs_ok])
    root = Agent(name="triage", handoffs=[base_duplicate, resumed_duplicate])
    base_duplicate.handoffs = [root]
    resumed_duplicate.handoffs = [root]

    state: RunState[dict[str, str], Agent[Any]] = RunState(
        context=RunContextWrapper(context={}),
        original_input="input",
        starting_agent=root,
        max_turns=2,
    )
    state._current_agent = resumed_duplicate
    state._current_step = NextStepInterruption(
        interruptions=[
            ToolApprovalItem(
                agent=resumed_duplicate,
                raw_item=cast(
                    ResponseFunctionToolCall,
                    get_function_tool_call(
                        "needs_ok",
                        json.dumps({"text": "one"}),
                        call_id="legacy-call",
                    ),
                ),
            )
        ]
    )
    state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    state._model_responses = [ModelResponse(output=[], usage=Usage(), response_id="resp")]

    json_data = state.to_json()
    current_agent_data = cast(dict[str, str], json_data["current_agent"])
    assert current_agent_data["name"] == "duplicate"
    assert "identity" in current_agent_data

    interruption_data = cast(
        dict[str, object],
        json_data["current_step"]["data"]["interruptions"][0],
    )
    interruption_agent_data = cast(dict[str, str], interruption_data["agent"])
    assert interruption_agent_data["identity"] == current_agent_data["identity"]
    interruption_agent_data.pop("identity")
    json_data["$schemaVersion"] = schema_version

    restored = await RunState.from_json(root, json_data)
    assert restored._schema_version == schema_version
    assert restored._current_agent is resumed_duplicate
    restored_approval = restored.get_interruptions()[0]
    restored.approve(restored_approval)
    assert restored._context is not None
    assert restored._last_processed_response is not None

    result = await turn_resolution.resolve_interrupted_turn(
        bindings=bind_public_agent(cast(Agent[dict[str, str]], restored._current_agent)),
        original_input=restored._original_input,
        original_pre_step_items=restored._generated_items,
        new_response=restored._model_responses[-1],
        processed_response=restored._last_processed_response,
        hooks=RunHooks(),
        context_wrapper=restored._context,
        run_config=RunConfig(),
        run_state=restored,
    )

    if expect_execution:
        assert isinstance(result.next_step, NextStepRunAgain)
        assert calls == ["one"]
        assert any(
            isinstance(item, ToolCallOutputItem) and item.output == "one"
            for item in result.new_step_items
        )
    else:
        assert calls == []
        assert not any(
            isinstance(item, ToolCallOutputItem) and item.output == "one"
            for item in result.new_step_items
        )
