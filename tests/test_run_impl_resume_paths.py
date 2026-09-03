import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

import agents.run as run_module
from agents import Agent, AgentUpdatedStreamEvent, Runner, function_tool, handoff
from agents.agent import ToolsToFinalOutputResult
from agents.agent_output import AgentOutputSchema
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail
from agents.exceptions import UserError
from agents.guardrail import GuardrailFunctionOutput, input_guardrail
from agents.items import (
    MessageOutputItem,
    ModelResponse,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from agents.lifecycle import RunHooks
from agents.memory import OpenAIResponsesCompactionSession, Session, SQLiteSession
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
from agents.tool import Tool
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
)
from agents.usage import Usage
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.hitl import (
    make_agent,
    make_context_wrapper,
    make_model_and_agent,
    queue_function_call_and_text,
)
from tests.utils.simple_session import SimpleListSession


class _FailingResumeSession(SimpleListSession):
    """Control append acknowledgement at the public Session boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None
        self.error = RuntimeError("session append failed")
        self.block_next_add = False
        self.add_started = asyncio.Event()
        self.release_add = asyncio.Event()

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error
        if self.block_next_add:
            self.block_next_add = False
            self.add_started.set()
            await self.release_add.wait()
        if failure == "partial":
            await super().add_items(items[:1])
            raise self.error
        await super().add_items(items)
        if failure == "after":
            raise self.error


class _FailSecondAddItemsSession(SimpleListSession):
    """Let the initial input-priming append succeed, then fail the next append.

    Unlike ``_FailingResumeSession``, this targets a specific append by call order rather than
    a resume-cycle phase, so it can isolate a fresh (non-resumed) run's first real turn save.
    """

    def __init__(self) -> None:
        super().__init__()
        self.error = RuntimeError("session append failed")
        self._call_count = 0

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self._call_count += 1
        if self._call_count == 2:
            raise self.error
        await super().add_items(items)


class _FailSecondAddItemsSessionWithYield(_FailSecondAddItemsSession):
    """Same failure shape as ``_FailSecondAddItemsSession``, but the failing call performs a
    genuine ``await`` (a scheduler yield) before raising, like a real I/O-backed Session
    (SQLite, network, etc.) would. A purely synchronous raise never yields control back to the
    ``stream_events()`` consumer before the run-loop task finishes, so a test built on it cannot
    observe whether an already-queued stream event was delivered before the error surfaced.
    """

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if self._call_count == 1:
            await asyncio.sleep(0)
        await super().add_items(items)


class _LostAckSQLiteSession(SQLiteSession):
    fail_after_commit = False
    error = RuntimeError("session append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        await super().add_items(items)
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise self.error


async def _run_session_resume(
    agent: Agent[Any],
    value: str | RunState[Any],
    session: Session | None,
    streamed: bool,
    hooks: RunHooks[Any] | None = None,
):
    config = RunConfig(tracing_disabled=True)
    if not streamed:
        return await Runner.run(agent, value, session=session, run_config=config, hooks=hooks)
    result = Runner.run_streamed(agent, value, session=session, run_config=config, hooks=hooks)
    async for _ in result.stream_events():
        pass
    return result


async def _approved_session_state(streamed: bool, session: Session | None = None):
    effects: list[int] = []

    @tool(needs_approval=True)
    async def charge(amount: int) -> str:
        effects.append(amount)
        return "receipt-7"

    model = ScriptedModel(
        [
            [get_function_tool_call("charge", '{"amount":7}', call_id="charge-1")],
            [get_text_message("done")],
            [get_text_message("fresh")],
        ]
    )
    agent = Agent(name="payment", model=model, tools=[charge])
    session = session if session is not None else _FailingResumeSession()
    paused = await _run_session_resume(agent, "charge 7", session, streamed)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    return agent, model, session, state, effects


def _charge_pair(items: list[TResponseInputItem]) -> list[str]:
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, dict) and item.get("call_id") == "charge-1"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_streamed,retry_streamed", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_resumed_session_append_is_recovered_before_next_model(
    failing_streamed: bool, retry_streamed: bool, round_trip: bool, failure: str
) -> None:
    agent, model, session, state, effects = await _approved_session_state(failing_streamed)
    session.failure = failure
    with pytest.raises(RuntimeError) as error:
        await _run_session_resume(agent, state, session, failing_streamed)
    assert error.value is session.error
    assert effects == [7]
    assert len(model.calls) == 1
    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    result = await _run_session_resume(agent, state, session, retry_streamed)
    assert result.final_output == "done"
    assert effects == [7]
    expected_pair = ["function_call", "function_call_output"]
    assert _charge_pair(await session.get_items()) == expected_pair
    assert _charge_pair(result.to_input_list()) == expected_pair
    await _run_session_resume(agent, "What was the receipt?", session, retry_streamed)
    assert _charge_pair(model.calls[-1].input) == expected_pair
    assert "pending_session_write" not in result.to_state().to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_streamed", [False, True])
@pytest.mark.parametrize("mismatch", ["missing", "different-id", "changed-tail"])
async def test_resumed_session_append_rejects_ambiguous_recovery(
    retry_streamed: bool, mismatch: str
) -> None:
    agent, model, session, state, effects = await _approved_session_state(False)
    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run_session_resume(agent, state, session, False)
    state = await RunState.from_json(agent, state.to_json())
    supplied_session: Session | None = session
    if mismatch == "missing":
        supplied_session = None
    elif mismatch == "different-id":
        supplied_session = SimpleListSession("other", await session.get_items())
    else:
        await session.add_items([{"role": "user", "content": "another writer"}])
    before = await session.get_items()
    with pytest.raises(UserError, match="pending Session write"):
        await _run_session_resume(agent, state, supplied_session, retry_streamed)
    assert len(model.calls) == 1
    assert effects == [7]
    assert await session.get_items() == before


@pytest.mark.asyncio
async def test_resumed_session_append_survives_repeated_failure_and_late_input() -> None:
    agent, model, session, state, effects = await _approved_session_state(False)
    for _ in range(2):
        session.failure = "before"
        with pytest.raises(RuntimeError, match="session append failed"):
            await _run_session_resume(agent, state, session, False)
        state = await RunState.from_json(agent, state.to_json())
        assert len(model.calls) == 1
        assert effects == [7]
    state.add_input("What was the receipt?")
    result = await _run_session_resume(agent, state, session, True)
    assert result.final_output == "done"
    stored = await session.get_items()
    output_index = next(
        i for i, item in enumerate(stored) if item.get("type") == "function_call_output"
    )
    late_index = next(
        i for i, item in enumerate(stored) if item.get("content") == "What was the receipt?"
    )
    assert output_index < late_index
    assert effects == [7]


async def _partially_approved_session_state(streamed: bool):
    """Pause on two approval-gated calls in one response and approve only the first."""
    effects: list[int] = []

    @tool(needs_approval=True)
    async def charge(amount: int) -> str:
        effects.append(amount)
        return "receipt-7"

    @tool(needs_approval=True)
    async def notify() -> str:
        raise AssertionError("the unresolved approval must not execute")

    model = ScriptedModel(
        [
            [
                get_function_tool_call("charge", '{"amount":7}', call_id="charge-1"),
                get_function_tool_call("notify", "{}", call_id="notify-1"),
            ],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="payment", model=model, tools=[charge, notify])
    session = _FailingResumeSession()
    paused = await _run_session_resume(agent, "charge 7 and notify", session, streamed)
    state = paused.to_state()
    charge_approval = next(
        item for item in state.get_interruptions() if item.raw_item.call_id == "charge-1"
    )
    state.approve(charge_approval)
    return agent, model, session, state, effects


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
async def test_renewed_interruption_recovers_failed_resumed_session_append(
    streamed: bool, round_trip: bool
) -> None:
    agent, model, session, state, effects = await _partially_approved_session_state(streamed)
    session.failure = "before"
    with pytest.raises(RuntimeError) as error:
        await _run_session_resume(agent, state, session, streamed)
    assert error.value is session.error
    assert effects == [7]
    assert _charge_pair(await session.get_items()) == ["function_call"]

    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    pending = await _run_session_resume(agent, state, session, streamed)
    pending_state = pending.to_state()
    remaining = pending_state.get_interruptions()
    assert [item.raw_item.call_id for item in remaining] == ["notify-1"]
    assert len(model.calls) == 1

    pending_state.reject(remaining[0], rejection_message="declined")
    result = await _run_session_resume(agent, pending_state, session, streamed)
    assert result.final_output == "done"
    assert effects == [7]
    expected_pair = ["function_call", "function_call_output"]
    assert _charge_pair(await session.get_items()) == expected_pair
    assert _charge_pair(result.to_input_list()) == expected_pair


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
async def test_resumed_committed_append_refreshes_compaction_input(
    streamed: bool, round_trip: bool, tmp_path: Path
) -> None:
    backend = _LostAckSQLiteSession("compaction-recovery", tmp_path / "history.db")
    compaction_inputs: list[list[TResponseInputItem]] = []
    compact_enabled = False

    async def compact(**kwargs: Any) -> SimpleNamespace:
        items = copy.deepcopy(kwargs["input"])
        compaction_inputs.append(items)
        return SimpleNamespace(output=items, usage=None)

    session = OpenAIResponsesCompactionSession(
        backend.session_id,
        underlying_session=backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=lambda _: compact_enabled,
    )
    try:
        agent, model, _, state, effects = await _approved_session_state(streamed, session)
        # A normal declined compaction initializes the retained wrapper's history cache.
        await session.run_compaction()
        assert compaction_inputs == []
        backend.fail_after_commit = True
        with pytest.raises(RuntimeError) as error:
            await _run_session_resume(agent, state, session, streamed)
        assert error.value is backend.error
        expected_pair = ["function_call", "function_call_output"]
        assert _charge_pair(await backend.get_items(limit=100)) == expected_pair
        if round_trip:
            state = await RunState.from_json(agent, state.to_json())

        compact_enabled = True
        result = await _run_session_resume(agent, state, session, streamed)
        assert result.final_output == "done"
        assert effects == [7]
        assert len(model.calls) == 2
        assert len(compaction_inputs) == 1
        assert _charge_pair(compaction_inputs[0]) == expected_pair
        assert _charge_pair(await backend.get_items(limit=100)) == expected_pair
        assert _charge_pair(result.to_input_list()) == expected_pair
        assert "pending_session_write" not in result.to_state().to_json()
    finally:
        backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["input", "auto"])
async def test_compaction_reload_preserves_session_retrieval_window(
    mode: Literal["input", "auto"], tmp_path: Path
) -> None:
    backend = _LostAckSQLiteSession(
        "bounded-compaction", tmp_path / "history.db", session_settings={"limit": 1}
    )
    compaction_inputs: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        assert "previous_response_id" not in kwargs
        items = copy.deepcopy(kwargs["input"])
        compaction_inputs.append(items)
        return SimpleNamespace(output=items, usage=None)

    session = OpenAIResponsesCompactionSession(
        backend.session_id,
        underlying_session=backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode=mode,
    )
    old_items: list[TResponseInputItem] = [
        {"role": "assistant", "content": f"old message {index}"} for index in range(12)
    ]
    recovered_item: TResponseInputItem = {"role": "assistant", "content": "committed reply"}
    try:
        await backend.add_items(old_items)
        # The configured window has one candidate, so the default threshold is not met.
        await session.run_compaction({"response_id": "unstored-response", "store": False})
        assert compaction_inputs == []
        assert await backend.get_items(limit=100) == old_items

        backend.fail_after_commit = True
        with pytest.raises(RuntimeError) as error:
            await session.add_items([recovered_item])
        assert error.value is backend.error
        assert await backend.get_items(limit=100) == [*old_items, recovered_item]

        await session.run_compaction({"force": True, "store": False})
        assert compaction_inputs == [[recovered_item]]
        assert await backend.get_items(limit=100) == [recovered_item]
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_cancelled_compaction_append_preserves_committed_and_surviving_writes() -> None:
    appended = asyncio.Event()
    wait_for_ack = asyncio.Event()

    class DelayedAckSession(SimpleListSession):
        delay_next_ack = True

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            await super().add_items(items)
            if self.delay_next_ack:
                self.delay_next_ack = False
                appended.set()
                await wait_for_ack.wait()

    backend = DelayedAckSession()
    compaction_inputs: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        items = copy.deepcopy(kwargs["input"])
        compaction_inputs.append(items)
        return SimpleNamespace(output=items, usage=None)

    session = OpenAIResponsesCompactionSession(
        backend.session_id,
        underlying_session=backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=lambda _: False,
    )
    await session.run_compaction()
    first_item: TResponseInputItem = {"role": "user", "content": "committed before cancellation"}
    newer_item: TResponseInputItem = {"role": "user", "content": "surviving writer"}
    first = asyncio.create_task(session.add_items([first_item]))
    newer: asyncio.Task[None] | None = None
    newer_started = asyncio.Event()

    async def write_newer() -> None:
        newer_started.set()
        await session.add_items([newer_item])

    try:
        await asyncio.wait_for(appended.wait(), timeout=5)
        newer = asyncio.create_task(write_newer())
        await asyncio.wait_for(newer_started.wait(), timeout=5)
        assert not newer.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(newer, timeout=5)
        await session.run_compaction({"force": True})
        assert compaction_inputs == [[first_item, newer_item]]
        assert await backend.get_items() == [first_item, newer_item]
    finally:
        wait_for_ack.set()
        tasks = [first, *([newer] if newer is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_resumed_session_append_cancellation_retains_recoverable_state(
    streamed: bool,
) -> None:
    agent, model, session, state, effects = await _approved_session_state(streamed)
    session.block_next_add = True
    attempt = asyncio.create_task(_run_session_resume(agent, state, session, streamed))
    try:
        await asyncio.wait_for(session.add_started.wait(), timeout=5)
        with pytest.raises(UserError, match="pending Session write is already in progress"):
            await _run_session_resume(agent, state, session, not streamed)
        assert len(model.calls) == 1
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
    finally:
        session.release_add.set()
        if not attempt.done():
            attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)

    restored = await RunState.from_json(agent, state.to_json())
    result = await _run_session_resume(agent, restored, session, not streamed)
    assert result.final_output == "done"
    assert effects == [7]
    assert _charge_pair(await session.get_items()) == ["function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_failed_streamed_result_checkpoint_retains_detached_pending_write() -> None:
    agent, model, session, state, effects = await _approved_session_state(True)
    session.failure = "before"
    result = Runner.run_streamed(agent, state, session=session)
    with pytest.raises(RuntimeError, match="session append failed"):
        async for _ in result.stream_events():
            pass
    snapshot = result.to_state()
    payload = snapshot.to_json()
    payload["pending_session_write"]["items"][0]["output"] = "changed snapshot"
    assert state.to_json()["pending_session_write"]["items"][0]["output"] == "receipt-7"
    assert snapshot.to_json()["pending_session_write"]["items"][0]["output"] == "receipt-7"
    await _run_session_resume(agent, snapshot, session, False)
    assert effects == [7]
    assert len(model.calls) == 2
    assert _charge_pair(await session.get_items()) == ["function_call", "function_call_output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["old-schema", "batch-shape"])
async def test_pending_session_write_rejects_invalid_serialized_checkpoint(invalid: str) -> None:
    agent, _, session, state, _ = await _approved_session_state(False)
    session.failure = "before"
    with pytest.raises(RuntimeError):
        await _run_session_resume(agent, state, session, False)
    payload = state.to_json()
    if invalid == "old-schema":
        payload["$schemaVersion"] = "1.16"
    else:
        payload["pending_session_write"]["items"] = "not an item batch"
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, payload)


@pytest.mark.asyncio
async def test_resumed_session_append_partial_commit_fails_closed() -> None:
    agent, model, session, state, effects = await _approved_session_state(False)
    # Two approved calls produce one resumed batch, allowing an actual partial append.
    second_call = get_function_tool_call("charge", '{"amount":7}', call_id="charge-2")
    model = ScriptedModel(
        [
            [get_function_tool_call("charge", '{"amount":7}', call_id="charge-1"), second_call],
            [get_text_message("done")],
        ]
    )
    agent.model = model
    session = _FailingResumeSession()
    paused = await _run_session_resume(agent, "charge twice", session, False)
    state = paused.to_state()
    for interruption in state.get_interruptions():
        state.approve(interruption)
    session.failure = "partial"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run_session_resume(agent, state, session, False)
    before = await session.get_items()
    restored = await RunState.from_json(agent, state.to_json())
    with pytest.raises(UserError, match="history changed or is ambiguous"):
        await _run_session_resume(agent, restored, session, True)
    assert effects == [7, 7]
    assert len(model.calls) == 1
    assert await session.get_items() == before


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


async def _approved_handoff_session_state(streamed: bool):
    """Pause on an approval-gated call that shares its response with a handoff."""
    effects: list[int] = []
    guardrail_calls: list[str] = []
    hook_calls: list[str] = []
    handoff_calls: list[str] = []

    class CountingHooks(RunHooks[Any]):
        async def on_tool_start(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
        ) -> None:
            hook_calls.append("tool-start")

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
            result: object,
        ) -> None:
            hook_calls.append("tool-end")

    @tool_input_guardrail
    def record_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        guardrail_calls.append("input")
        return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

    @tool_output_guardrail
    def record_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        guardrail_calls.append("output")
        return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

    @tool(
        needs_approval=True,
        tool_input_guardrails=[record_input],
        tool_output_guardrails=[record_output],
    )
    async def charge(amount: int) -> str:
        effects.append(amount)
        return "receipt-7"

    model = ScriptedModel(
        [
            [
                get_function_tool_call("charge", '{"amount":7}', call_id="charge-1"),
                get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1"),
            ],
            [get_text_message("done")],
            [get_text_message("fresh")],
        ]
    )
    delegate = Agent(name="delegate", model=model)
    route = handoff(delegate, on_handoff=lambda _context: handoff_calls.append("handoff"))
    agent = Agent(name="triage", model=model, tools=[charge], handoffs=[route])
    hooks = CountingHooks()
    session = _FailingResumeSession()
    paused = await _run_session_resume(
        agent,
        "charge 7 then hand off",
        session,
        streamed,
        hooks,
    )
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    return agent, model, session, state, effects, guardrail_calls, hook_calls, handoff_calls, hooks


def _call_pair(items: list[TResponseInputItem], call_id: str) -> list[str]:
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, dict) and item.get("call_id") == call_id
    ]


def _guardrail_output_info(state: RunState[Any]) -> tuple[list[Any], list[Any]]:
    return (
        [item.output.output_info for item in state._tool_input_guardrail_results],
        [item.output.output_info for item in state._tool_output_guardrail_results],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_streamed,retry_streamed", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_resumed_handoff_session_append_is_recovered_before_next_model(
    failing_streamed: bool, retry_streamed: bool, round_trip: bool, failure: str
) -> None:
    (
        agent,
        model,
        session,
        state,
        effects,
        guardrail_calls,
        hook_calls,
        handoff_calls,
        hooks,
    ) = await _approved_handoff_session_state(failing_streamed)
    session.failure = failure
    if failing_streamed:
        failed_result = Runner.run_streamed(
            agent,
            state,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
            hooks=hooks,
        )
        with pytest.raises(RuntimeError) as error:
            async for _ in failed_result.stream_events():
                pass
        state = failed_result.to_state()
    else:
        with pytest.raises(RuntimeError) as error:
            await _run_session_resume(agent, state, session, False, hooks)
    assert error.value is session.error
    assert effects == [7]
    assert guardrail_calls == ["input", "output"]
    assert hook_calls == ["tool-start", "tool-end"]
    assert handoff_calls == ["handoff"]
    assert len(model.calls) == 1
    assert _guardrail_output_info(state) == (["input-checked"], ["output-checked"])
    failed_payload = state.to_json()
    pending_write = cast(dict[str, Any], failed_payload["pending_session_write"])
    pending_items = cast(list[TResponseInputItem], pending_write["items"])
    assert _call_pair(pending_items, "charge-1") == ["function_call_output"]
    assert _call_pair(pending_items, "handoff-1") == ["function_call_output"]
    if round_trip:
        state = await RunState.from_json(agent, failed_payload)
        assert _guardrail_output_info(state) == (["input-checked"], ["output-checked"])
    assert state._current_agent is not None and state._current_agent.name == "delegate"

    result = await _run_session_resume(agent, state, session, retry_streamed, hooks)
    assert result.final_output == "done"
    assert result.last_agent.name == "delegate"
    assert effects == [7]
    assert guardrail_calls == ["input", "output"]
    assert hook_calls == ["tool-start", "tool-end"]
    assert handoff_calls == ["handoff"]
    assert [item.output.output_info for item in result.tool_input_guardrail_results] == [
        "input-checked"
    ]
    assert [item.output.output_info for item in result.tool_output_guardrail_results] == [
        "output-checked"
    ]
    assert len(model.calls) == 2
    expected_pair = ["function_call", "function_call_output"]
    stored = await session.get_items()
    assert _call_pair(stored, "charge-1") == expected_pair
    assert _call_pair(stored, "handoff-1") == expected_pair
    assert _call_pair(result.to_input_list(), "charge-1") == expected_pair
    assert _call_pair(result.to_input_list(), "handoff-1") == expected_pair
    assert "pending_session_write" not in result.to_state().to_json()


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_preserves_agent_after_session_append_failure() -> None:
    """A fresh (non-resumed) streamed run's generic-loop handoff branch must publish the new
    agent and next-step state before the fallible session append, mirroring the fix already
    applied to the is_resumed_state branch covered by
    test_resumed_handoff_session_append_is_recovered_before_next_model. Every fresh streamed
    run passes through this branch, not just resumed ones.
    """
    model = ScriptedModel(
        [
            [get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")],
            [get_text_message("done")],
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(name="triage", model=model, handoffs=[delegate])
    session = _FailSecondAddItemsSession()

    failed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    with pytest.raises(RuntimeError) as error:
        async for _ in failed_result.stream_events():
            pass
    assert error.value is session.error

    state = failed_result.to_state()
    assert state._current_agent is not None
    assert state._current_agent.name == "delegate"
    assert failed_result.current_agent.name == "delegate"

    result = await _run_session_resume(triage, state, session, False)
    assert result.final_output == "done"
    assert result.last_agent.name == "delegate"
    assert len(model.calls) == 2
    expected_pair = ["function_call", "function_call_output"]
    stored = await session.get_items()
    assert _call_pair(stored, "handoff-1") == expected_pair
    assert "pending_session_write" not in result.to_state().to_json()


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_publishes_agent_update_before_session_append_failure() -> (
    None
):
    """The generic-loop handoff branch must queue ``AgentUpdatedStreamEvent`` for the new agent
    before the fallible session append, so ``stream_events()`` consumers observe the transition
    even when the append later raises. Mirrors the already-merged ordering fix for the
    ``is_resumed_state``-specific branch (lines ~1407-1437), which is out of scope here.

    Uses a session whose failing append performs a genuine ``await`` before raising: a purely
    synchronous raise (as in ``_FailSecondAddItemsSession``) never yields control back to this
    consumer before the run-loop task finishes, so it cannot prove event delivery either way
    (a separate, pre-existing gate: ``stream_events()`` only drains an already-queued event past
    a terminal error when that error was marked via ``_mark_error_to_drain_stream_events()``,
    which session-append failures never are).
    """
    model = ScriptedModel(
        [
            [get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")],
            [get_text_message("done")],
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(name="triage", model=model, handoffs=[delegate])
    session = _FailSecondAddItemsSessionWithYield()

    failed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    collected_events: list[Any] = []
    caught: RuntimeError | None = None
    try:
        async for event in failed_result.stream_events():
            collected_events.append(event)
    except RuntimeError as error:
        caught = error
    assert caught is session.error
    assert any(
        isinstance(event, AgentUpdatedStreamEvent) and event.new_agent.name == "delegate"
        for event in collected_events
    )


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_drains_agent_update_event_for_slow_consumer() -> None:
    """A session-append failure in the generic-loop handoff branch must mark itself for
    stream-event draining, so a consumer that falls even slightly behind the producer (an
    ordinary per-event delay, not a contrived zero-delay reader) still observes the
    already-queued ``AgentUpdatedStreamEvent`` before the error surfaces.

    test_fresh_streamed_handoff_publishes_agent_update_before_session_append_failure's
    zero-delay consumer passes even without draining, since it never falls behind the
    producer; this test exercises the actual drain guarantee stream_events() provides via
    _mark_error_to_drain_stream_events()/_should_drain_stream_events_before_raising().
    """
    model = ScriptedModel(
        [
            [get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")],
            [get_text_message("done")],
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(name="triage", model=model, handoffs=[delegate])
    session = _FailSecondAddItemsSessionWithYield()

    failed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    collected_events: list[Any] = []
    caught: RuntimeError | None = None
    try:
        async for event in failed_result.stream_events():
            # An ordinary bit of per-event consumer work, enough to fall behind the producer.
            await asyncio.sleep(0.001)
            collected_events.append(event)
    except RuntimeError as error:
        caught = error
    assert caught is session.error
    assert any(
        isinstance(event, AgentUpdatedStreamEvent) and event.new_agent.name == "delegate"
        for event in collected_events
    )


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_awaits_parallel_input_guardrail_before_transition() -> None:
    """The generic-loop handoff branch must resolve an in-flight parallel input guardrail
    before committing the handoff transition (current_agent, run_state, published events),
    per the Guardrail Ordering contract in .agents/references/runner-lifecycle.md. Otherwise a
    handoff on turn 1 can commit the transition while a still-running parallel input guardrail
    that later raises has not yet been awaited.

    Only one scripted turn is provided (the handoff itself), and the consumer adds a small
    per-event delay: an in-process run with an instantly-draining consumer and a second
    scripted turn can otherwise race straight through to completion before the guardrail's
    sleep elapses, defeating the repro regardless of the fix. This mirrors an ordinary
    consumer that does a bit of per-event work, not a contrived instant reader.
    """
    guardrail_error = RuntimeError("guardrail backend exploded")

    @input_guardrail(run_in_parallel=True)
    async def slow_failing_guardrail(
        ctx: RunContextWrapper[Any],
        agent: Agent[Any],
        input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0.3)
        raise guardrail_error

    model = ScriptedModel(
        [
            [get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")],
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
        input_guardrails=[slow_failing_guardrail],
    )

    streamed_result = Runner.run_streamed(
        triage, "hello", run_config=RunConfig(tracing_disabled=True)
    )
    caught: RuntimeError | None = None
    try:
        async for _ in streamed_result.stream_events():
            await asyncio.sleep(0.05)
    except RuntimeError as error:
        caught = error
    assert caught is guardrail_error
    # The handoff transition must not have been committed: the guardrail task was still
    # in flight (sleeping) when the model returned the handoff, and it raised a real error
    # rather than a tripwire, so no part of the observable state should have moved past triage.
    assert streamed_result.current_agent.name == "triage"
    state = streamed_result.to_state()
    assert state._current_agent is not None
    assert state._current_agent.name == "triage"


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_replays_deferred_compaction_after_resume() -> None:
    """A checkpointed handoff batch that fails to append and later settles via a separate,
    standalone resume_pending_session_write() call (the generic resume-startup path in
    run.py/run_loop.py, not the original save_result_to_session() call) must still apply the
    same post-write Responses compaction decision save_result_to_session would have applied
    inline, instead of silently and permanently losing it. See
    .agents/references/session-persistence.md.

    Uses a should_trigger_compaction hook keyed on response_id (as a caller doing per-turn
    compaction routing would) to make the loss observable: without the fix, the handoff's own
    response_id is never evaluated by the hook at all, and the deferral it would have set is
    never recorded, so the later forced compaction on the delegate's turn never happens either.
    """
    hook_calls: list[str | None] = []

    def should_trigger_compaction(context: dict[str, Any]) -> bool:
        hook_calls.append(context["response_id"])
        return context["response_id"] == "resp-handoff"

    compact_calls: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        items = copy.deepcopy(kwargs["input"])
        compact_calls.append(items)
        return SimpleNamespace(output=items, usage=None)

    backend = _FailSecondAddItemsSession()
    session = OpenAIResponsesCompactionSession(
        "compaction-handoff-test",
        underlying_session=backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=should_trigger_compaction,
    )

    model = ScriptedModel(
        [
            {
                "output": [
                    get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")
                ],
                "response_id": "resp-handoff",
            },
            {"output": [get_text_message("done")], "response_id": "resp-delegate"},
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(name="triage", model=model, handoffs=[delegate])

    failed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    with pytest.raises(RuntimeError) as error:
        async for _ in failed_result.stream_events():
            pass
    assert error.value is backend.error
    assert hook_calls == []
    assert compact_calls == []
    state = failed_result.to_state()
    assert state._pending_session_write is not None
    assert state._pending_session_write.get("response_id") == "resp-handoff"
    assert state._pending_session_write.get("has_local_tool_outputs") is True

    result = await _run_session_resume(triage, state, session, False)
    assert result.final_output == "done"
    # The handoff's own response_id must have been evaluated by the decision hook (and
    # deferred), not skipped -- and, because force-compaction short-circuits the hook, it must
    # be the only response_id the hook ever saw.
    assert hook_calls == ["resp-handoff"]
    # The deferred decision must actually have been forced through on the delegate's own save,
    # i.e. the compact API was invoked at all -- not just checked and declined.
    assert len(compact_calls) == 1


@pytest.mark.asyncio
async def test_fresh_streamed_handoff_retains_checkpoint_when_post_write_compaction_fails() -> None:
    """If the post-write compaction decision raises after a checkpointed handoff batch's append
    has already settled, the checkpoint (``_pending_session_write``) must survive so a later
    retry can redo just the compaction step -- clearing it before the fallible compaction call
    would silently and permanently lose the requested deferred/forced compaction with no way to
    recover it. See .agents/references/session-persistence.md.
    """
    hook_calls: list[str | None] = []
    compaction_error = RuntimeError("compaction decision hook exploded")
    should_fail = True

    def should_trigger_compaction(context: dict[str, Any]) -> bool:
        hook_calls.append(context["response_id"])
        if context["response_id"] == "resp-handoff" and should_fail:
            raise compaction_error
        return context["response_id"] == "resp-handoff"

    compact_calls: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        items = copy.deepcopy(kwargs["input"])
        compact_calls.append(items)
        return SimpleNamespace(output=items, usage=None)

    backend = _FailSecondAddItemsSession()
    session = OpenAIResponsesCompactionSession(
        "compaction-handoff-failure-test",
        underlying_session=backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=should_trigger_compaction,
    )

    model = ScriptedModel(
        [
            {
                "output": [
                    get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")
                ],
                "response_id": "resp-handoff",
            },
            {"output": [get_text_message("done")], "response_id": "resp-delegate"},
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(name="triage", model=model, handoffs=[delegate])

    failed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    with pytest.raises(RuntimeError) as append_error:
        async for _ in failed_result.stream_events():
            pass
    assert append_error.value is backend.error
    state = failed_result.to_state()
    assert state._pending_session_write is not None

    # Resume: the append itself now succeeds (the backend's failure was one-shot), but the
    # compaction decision hook raises for the handoff's own response_id.
    with pytest.raises(RuntimeError) as compaction_error_info:
        await _run_session_resume(triage, state, session, False)
    assert compaction_error_info.value is compaction_error
    # The checkpoint must still be present so a later retry can redo compaction alone, instead
    # of the handoff's requested compaction being silently and permanently lost.
    assert state._pending_session_write is not None
    assert state._pending_session_write.get("response_id") == "resp-handoff"

    # Retry: the hook no longer fails. The append must not be repeated (no duplicate items in
    # session history), but compaction must actually run this time.
    should_fail = False
    hook_calls.clear()
    result = await _run_session_resume(triage, state, session, False)
    assert result.final_output == "done"
    assert hook_calls == ["resp-handoff"]
    assert len(compact_calls) == 1
    stored = await session.get_items()
    handoff_pair = [
        str(item.get("type"))
        for item in stored
        if isinstance(item, dict) and item.get("call_id") == "handoff-1"
    ]
    assert handoff_pair == ["function_call", "function_call_output"]
