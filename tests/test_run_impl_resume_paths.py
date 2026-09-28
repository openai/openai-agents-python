import asyncio
import copy
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

import agents.run as run_module
from agents import Agent, AgentUpdatedStreamEvent, Runner, function_tool, handoff, output_guardrail
from agents.agent import ToolsToFinalOutputResult
from agents.agent_output import AgentOutputSchema
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail
from agents.exceptions import InputGuardrailTripwireTriggered, UserError
from agents.guardrail import GuardrailFunctionOutput, input_guardrail
from agents.handoffs import HandoffInputData
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
from agents.sandbox.runtime import SandboxRuntime
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
        self.fail_on_output: str | None = None

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if self.fail_on_output is not None and any(
            self.fail_on_output in json.dumps(item, default=str) for item in items
        ):
            self.fail_on_output = None
            raise self.error
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
@pytest.mark.parametrize(
    "invalid",
    [
        "old-schema",
        "batch-shape",
        "compaction-under-1.17",
        "acknowledgement-type",
        "acknowledgement-without-before",
        "compaction-exchange-type",
        "compaction-exchange-policy",
        "acknowledgement-under-1.17",
    ],
)
async def test_pending_session_write_rejects_invalid_serialized_checkpoint(invalid: str) -> None:
    agent, _, session, state, _ = await _approved_session_state(False)
    session.failure = "before"
    with pytest.raises(RuntimeError):
        await _run_session_resume(agent, state, session, False)
    payload = state.to_json()
    if invalid in {"old-schema", "compaction-under-1.17", "acknowledgement-under-1.17"}:
        for entry in payload["context"].pop("function_tool_approvals", []):
            payload["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    if invalid == "old-schema":
        payload["$schemaVersion"] = "1.16"
    elif invalid == "compaction-under-1.17":
        payload["$schemaVersion"] = "1.17"
    elif invalid == "acknowledgement-type":
        payload["pending_session_write"]["append_acknowledged"] = "true"
    elif invalid == "acknowledgement-without-before":
        payload["pending_session_write"]["append_acknowledged"] = True
        payload["pending_session_write"]["before"] = None
    elif invalid == "compaction-exchange-type":
        payload["pending_session_write"]["compaction_model_exchange"] = {
            "item_digests": "not a digest list",
            "reasoning_item_id_policy": None,
        }
    elif invalid == "compaction-exchange-policy":
        payload["pending_session_write"]["compaction_model_exchange"] = {
            "item_digests": [],
            "reasoning_item_id_policy": "unknown",
        }
    elif invalid == "acknowledgement-under-1.17":
        payload["$schemaVersion"] = "1.17"
        for key in ("response_id", "store", "has_local_tool_outputs"):
            payload["pending_session_write"].pop(key)
        payload["pending_session_write"]["append_acknowledged"] = True
    else:
        payload["pending_session_write"]["items"] = "not an item batch"
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, payload)


@pytest.mark.asyncio
async def test_legacy_pending_session_write_still_resumes_without_compaction_metadata() -> None:
    agent, model, session, state, effects = await _approved_session_state(False)
    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run_session_resume(agent, state, session, False)
    payload = state.to_json()
    payload["$schemaVersion"] = "1.17"
    for entry in payload["context"].pop("function_tool_approvals", []):
        payload["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    for key in ("response_id", "store", "has_local_tool_outputs"):
        payload["pending_session_write"].pop(key)
    restored = await RunState.from_json(agent, payload)
    result = await _run_session_resume(agent, restored, session, False)
    assert result.final_output == "done"
    assert effects == [7]
    assert len(model.calls) == 2
    assert _charge_pair(await session.get_items()) == ["function_call", "function_call_output"]


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


def _sent_tool_outputs(model: ScriptedModel, *, first_call_index: int) -> list[tuple[str, str]]:
    """Collect the tool outputs the model received, from `first_call_index` onward."""
    outputs: list[tuple[str, str]] = []
    for call in model.calls[first_call_index:]:
        for item in cast(list[dict[str, Any]], call.input):
            if item.get("type") == "function_call_output":
                outputs.append((str(item.get("call_id")), str(item.get("output"))))
    return outputs


async def _run_server_managed(
    agent: Agent[Any],
    agent_input: Any,
    *,
    run_config: RunConfig,
    use_conversation_id: bool,
    streaming: bool,
) -> Any:
    """Run the agent under one of the server-managed continuation modes."""
    kwargs: dict[str, Any] = (
        {"conversation_id": "conv-resume"}
        if use_conversation_id
        else {"auto_previous_response_id": True}
    )
    if streaming:
        streamed = Runner.run_streamed(agent, agent_input, run_config=run_config, **kwargs)
        async for _ in streamed.stream_events():
            pass
        return streamed
    return await Runner.run(agent, agent_input, run_config=run_config, **kwargs)


@pytest.mark.parametrize("streaming", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.parametrize("serialize_state", [False, True], ids=["live_state", "serialized_state"])
@pytest.mark.parametrize(
    "use_conversation_id", [False, True], ids=["auto_previous_response_id", "conversation_id"]
)
@pytest.mark.asyncio
async def test_resumed_server_managed_run_sends_tool_not_found_output(
    streaming: bool,
    serialize_state: bool,
    use_conversation_id: bool,
) -> None:
    """A resumed server-managed run must send the output built for a missing tool.

    The interrupted turn answers the unknown tool locally while another call waits for
    approval. The server already owns both calls, so resuming has to deliver both outputs.
    """

    @function_tool(name_override="needs_ok", needs_approval=True)
    async def needs_ok(text: str) -> str:
        return text

    model = ScriptedModel()
    agent = Agent(name="test", model=model, tools=[needs_ok])
    model.extend(
        [
            [
                get_function_tool_call(
                    "needs_ok", json.dumps({"text": "one"}), call_id="call-approval"
                ),
                get_function_tool_call("missing_tool", json.dumps({}), call_id="call-missing"),
            ],
            [get_text_message("done")],
        ]
    )
    run_config = RunConfig(tool_not_found_behavior="return_error_to_model")

    async def run_once(agent_input: Any) -> Any:
        return await _run_server_managed(
            agent,
            agent_input,
            run_config=run_config,
            use_conversation_id=use_conversation_id,
            streaming=streaming,
        )

    first = await run_once("Use needs_ok and missing_tool")
    state = first.to_state()
    if serialize_state:
        state = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))
    interruptions = state.get_interruptions()
    assert [item.raw_item.call_id for item in interruptions] == ["call-approval"]
    state.approve(interruptions[0])

    resumed = await run_once(state)

    assert resumed.final_output == "done"
    delivered = _sent_tool_outputs(model, first_call_index=1)
    assert sorted(call_id for call_id, _ in delivered) == ["call-approval", "call-missing"]
    assert "missing_tool" in dict(delivered)["call-missing"]


@pytest.mark.asyncio
async def test_resumed_server_managed_run_sends_each_tool_output_once() -> None:
    """Staged approvals must deliver every output exactly once to a server-managed conversation.

    Approving one of two gated calls resumes and interrupts again without a model request, so
    the same model response stays current across both resumes.
    """

    @function_tool(name_override="needs_ok", needs_approval=True)
    async def needs_ok(text: str) -> str:
        return f"ok:{text}"

    model = ScriptedModel()
    agent = Agent(name="test", model=model, tools=[needs_ok])
    model.extend(
        [
            [
                get_function_tool_call("needs_ok", json.dumps({"text": "a"}), call_id="call-a"),
                get_function_tool_call("needs_ok", json.dumps({"text": "b"}), call_id="call-b"),
                get_function_tool_call("missing_tool", json.dumps({}), call_id="call-missing"),
            ],
            [get_text_message("done")],
        ]
    )
    run_config = RunConfig(tool_not_found_behavior="return_error_to_model")

    async def run_once(agent_input: Any) -> Any:
        return await _run_server_managed(
            agent,
            agent_input,
            run_config=run_config,
            use_conversation_id=False,
            streaming=False,
        )

    result = await run_once("Use needs_ok twice and missing_tool")
    for expected_model_calls in (1, 2):
        state = await RunState.from_json(agent, json.loads(json.dumps(result.to_state().to_json())))
        interruptions = state.get_interruptions()
        assert interruptions
        state.approve(interruptions[0])
        result = await run_once(state)
        # Approving only the first gated call resumes without asking the model again.
        assert len(model.calls) == expected_model_calls

    assert result.final_output == "done"
    delivered = _sent_tool_outputs(model, first_call_index=1)
    assert sorted(call_id for call_id, _ in delivered) == ["call-a", "call-b", "call-missing"]


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
    "decision",
    ["approve", "reject", "always_approve", "always_reject", "legacy_approve", "legacy_reject"],
)
@pytest.mark.parametrize(
    ("schema_version", "expect_execution"),
    [("1.6", True), ("1.7", False)],
)
async def test_resolve_interrupted_turn_only_uses_name_fallback_for_legacy_approval_agents(
    schema_version: str,
    expect_execution: bool,
    decision: str,
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
    if schema_version != "1.18":
        for entry in json_data["context"].pop("function_tool_approvals", []):
            json_data["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    json_data["$schemaVersion"] = schema_version
    if decision.startswith("legacy_"):
        json_data["context"]["approvals"]["needs_ok"] = {
            "approved": decision == "legacy_approve",
            "rejected": decision == "legacy_reject",
            "sticky_rejection_message": "Old unowned rejection",
        }

    restored = await RunState.from_json(root, json_data)
    assert restored._schema_version == schema_version
    assert restored._current_agent is resumed_duplicate
    restored_approval = restored.get_interruptions()[0]
    if decision in ("approve", "always_approve"):
        restored.approve(restored_approval, always_approve=decision == "always_approve")
    elif decision in ("reject", "always_reject"):
        restored.reject(
            restored_approval,
            always_reject=decision == "always_reject",
            rejection_message="Legacy exact rejection",
        )
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

    if expect_execution and decision in ("approve", "always_approve"):
        assert isinstance(result.next_step, NextStepRunAgain)
        assert calls == ["one"]
        assert any(
            isinstance(item, ToolCallOutputItem) and item.output == "one"
            for item in result.new_step_items
        )
    elif expect_execution and decision in ("reject", "always_reject"):
        assert isinstance(result.next_step, NextStepRunAgain)
        assert calls == []
        assert any(
            isinstance(item, ToolCallOutputItem) and item.output == "Legacy exact rejection"
            for item in result.new_step_items
        )
    else:
        assert calls == []
        assert not any(
            isinstance(item, ToolCallOutputItem) and item.output == "one"
            for item in result.new_step_items
        )

    if schema_version == "1.6" and decision.startswith("legacy_"):
        assert isinstance(result.next_step, NextStepInterruption)

    future = ToolApprovalItem(
        agent=resumed_duplicate,
        raw_item=get_function_tool_call("needs_ok", json.dumps({"text": "two"}), call_id="future"),
    )
    # Reconciliation honors the current decision without moving future scope.
    assert (
        restored._context.get_approval_status("needs_ok", "future", current_invocation=future)
        is None
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
    """A yielding Session failure still delivers the completed handoff's agent update."""
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
@pytest.mark.parametrize("raises_error", [False, True], ids=["tripwire", "guardrail-error"])
@pytest.mark.parametrize("retry_streamed", [False, True])
async def test_fresh_streamed_handoff_failed_guardrail_state_cannot_resume(
    raises_error: bool,
    retry_streamed: bool,
) -> None:
    handoff_observed = asyncio.Event()
    guardrail_error = RuntimeError("guardrail backend failed")

    @input_guardrail(run_in_parallel=True)
    async def pending_guardrail(
        ctx: RunContextWrapper[Any],
        agent: Agent[Any],
        input: str | list[TResponseInputItem],
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(handoff_observed.wait(), timeout=5)
        if raises_error:
            raise guardrail_error
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = ScriptedModel(
        [[get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1")]]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(
        name="triage", model=model, handoffs=[delegate], input_guardrails=[pending_guardrail]
    )
    session = SimpleListSession()
    streamed_result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    events: list[Any] = []
    expected_error = RuntimeError if raises_error else InputGuardrailTripwireTriggered
    with pytest.raises(expected_error):
        async for event in streamed_result.stream_events():
            events.append(event)
            if getattr(event, "name", None) == "handoff_occured":
                handoff_observed.set()
                await asyncio.sleep(0)
    assert handoff_observed.is_set()
    assert streamed_result.current_agent is triage
    assert not any(
        isinstance(event, AgentUpdatedStreamEvent) and event.new_agent is delegate
        for event in events
    )
    state = streamed_result.to_state()
    assert state.to_json()["terminal_unrecoverable"] is True
    restored = await RunState.from_json(triage, state.to_json())
    assert restored._current_agent is triage
    for checkpoint in (state, restored):
        with pytest.raises(UserError, match="cannot be resumed"):
            await _run_session_resume(triage, checkpoint, session, retry_streamed)
    assert len(model.calls) == 1
    assert _call_pair(await session.get_items(), "handoff-1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
async def test_fresh_streamed_handoff_replays_deferred_compaction_after_resume(
    round_trip: bool,
) -> None:
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

    if round_trip:
        payload = state.to_json()
        assert payload["$schemaVersion"] == "1.18"
        state = await RunState.from_json(triage, payload)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("retry_streamed", [False, True], ids=["run", "streamed"])
async def test_handoff_resume_after_cancelled_compaction_commits(
    round_trip: bool, retry_streamed: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control SQLite's worker before commit while exercising public runner cancellation."""
    backend = SQLiteSession("cancelled-compaction", tmp_path / "session.db")
    replacement_started = asyncio.Event()
    release_replacement = threading.Event()
    loop = asyncio.get_running_loop()
    compacted: list[TResponseInputItem] = [{"role": "assistant", "content": "compacted history"}]
    batches: list[list[TResponseInputItem]] = []
    original_insert = backend._insert_items

    def insert_items(conn: sqlite3.Connection, items: list[TResponseInputItem]) -> None:
        original_insert(conn, items)
        batches.append(copy.deepcopy(items))
        if items == compacted:
            loop.call_soon_threadsafe(replacement_started.set)
            assert release_replacement.wait(timeout=10)

    monkeypatch.setattr(backend, "_insert_items", insert_items)
    compact_calls = 0

    async def compact(**kwargs: Any) -> SimpleNamespace:
        nonlocal compact_calls
        compact_calls += 1
        return SimpleNamespace(output=compacted, usage=None)

    session = OpenAIResponsesCompactionSession(
        "cancelled-compaction",
        backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=lambda context: context["response_id"] == "resp-handoff",
    )
    handoff_calls: list[str] = []

    def retain_message(data: HandoffInputData) -> HandoffInputData:
        return data.clone(
            new_items=tuple(item for item in data.new_items if isinstance(item, MessageOutputItem))
        )

    model = ScriptedModel(
        [
            {
                "output": [
                    get_text_message("delegating"),
                    get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1"),
                ],
                "response_id": "resp-handoff",
            },
            {"output": [get_text_message("done")], "response_id": "resp-delegate"},
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[
            handoff(
                delegate,
                input_filter=retain_message,
                on_handoff=lambda _: handoff_calls.append("handoff"),
            )
        ],
    )
    result = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )

    async def drain() -> None:
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(replacement_started.wait(), timeout=10)
        result.cancel()
        release_replacement.set()
        await asyncio.wait_for(consumer, timeout=10)
        assert await session.get_items() == compacted
        assert len(model.calls) == 1
        state = result.to_state()
        assert state._pending_session_write is None
        if round_trip:
            state = await RunState.from_json(triage, json.loads(state.to_string()))
        resumed = await _run_session_resume(triage, state, session, retry_streamed)
        assert resumed.final_output == "done"
        assert len(model.calls) == 2
        assert handoff_calls == ["handoff"]
        assert compact_calls == 1
        assert len(batches) == 4  # Initial input, handoff message, replacement, final output.
        assert await session.get_items() == compacted + [resumed.new_items[-1].to_input_item()]
        assert "pending_session_write" not in resumed.to_state().to_json()
    finally:
        release_replacement.set()
        result.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("restore_failure", [None, "empty", "partial"])
async def test_handoff_resume_retries_rolled_back_compaction(
    retry_streamed: bool, round_trip: bool, restore_failure: str | None
) -> None:
    """A legacy Session rollback must retain compaction ownership without repeating the append."""
    compacted: list[TResponseInputItem] = [{"role": "assistant", "content": "compacted history"}]

    class FailReplacementSession(SimpleListSession):
        fail = True

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            if items == compacted and self.fail:
                self.fail = False
                raise RuntimeError("replacement failed before commit")
            if not self.fail and restore_failure is not None:
                if restore_failure == "partial":
                    await super().add_items(items[:1])
                raise RuntimeError("restoration failed")
            await super().add_items(items)

    backend = FailReplacementSession()
    compact_calls: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        compact_calls.append(copy.deepcopy(kwargs["input"]))
        return SimpleNamespace(output=compacted, usage=None)

    session = OpenAIResponsesCompactionSession(
        "rollback-compaction",
        backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=lambda context: context["response_id"] == "resp-handoff",
    )
    handoff_calls: list[str] = []

    def retain_message(data: HandoffInputData) -> HandoffInputData:
        return data.clone(
            new_items=tuple(item for item in data.new_items if isinstance(item, MessageOutputItem))
        )

    model = ScriptedModel(
        [
            {
                "output": [
                    get_text_message("delegating"),
                    get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1"),
                ],
                "response_id": "resp-handoff",
            },
            {"output": [get_text_message("done")], "response_id": "resp-delegate"},
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[
            handoff(
                delegate,
                input_filter=retain_message,
                on_handoff=lambda _: handoff_calls.append("handoff"),
            )
        ],
    )
    failed = Runner.run_streamed(
        triage, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    with pytest.raises(RuntimeError, match="replacement failed before commit"):
        async for _ in failed.stream_events():
            pass
    restored_history = await session.get_items()
    assert len(model.calls) == 1
    state = failed.to_state()
    if round_trip:
        state = await RunState.from_json(triage, json.loads(state.to_string()))
    if restore_failure is not None:
        assert len(restored_history) == (1 if restore_failure == "partial" else 0)
        with pytest.raises(UserError, match="Cannot reconcile the pending Session write"):
            await _run_session_resume(triage, state, session, retry_streamed)
        assert state._pending_session_write is not None
        assert state._pending_session_write["append_acknowledged"] is True
        assert await session.get_items() == restored_history
        assert len(model.calls) == 1
        assert len(compact_calls) == 1
        assert handoff_calls == ["handoff"]
        return
    assert len(restored_history) == 2
    resumed = await _run_session_resume(triage, state, session, retry_streamed)
    assert resumed.final_output == "done"
    assert compact_calls == [restored_history, restored_history]
    assert handoff_calls == ["handoff"]
    assert len(model.calls) == 2
    assert await session.get_items() == compacted + [resumed.new_items[-1].to_input_item()]
    assert "pending_session_write" not in resumed.to_state().to_json()


@pytest.mark.asyncio
async def test_json_compaction_retry_does_not_promote_filtered_session_history() -> None:
    backend = _FailingResumeSession()
    hidden: TResponseInputItem = {"role": "user", "content": "synthetic omitted history"}
    await backend.add_items([hidden])
    backend.fail_on_output = "delegating"
    compact_calls: list[list[TResponseInputItem]] = []

    async def compact(**kwargs: Any) -> SimpleNamespace:
        compact_calls.append(kwargs["input"])
        return SimpleNamespace(output=[], usage=None)

    session = OpenAIResponsesCompactionSession(
        "filtered-retry",
        backend,
        client=cast(Any, SimpleNamespace(responses=SimpleNamespace(compact=compact))),
        compaction_mode="input",
        should_trigger_compaction=lambda context: context["response_id"] == "resp-handoff",
    )
    model = ScriptedModel(
        [
            {
                "output": [
                    get_text_message("delegating"),
                    get_function_tool_call("transfer_to_delegate", "{}", call_id="handoff-1"),
                ],
                "response_id": "resp-handoff",
            },
            {"output": [get_text_message("done")], "response_id": "resp-delegate"},
        ]
    )
    delegate = Agent(name="delegate", model=model)
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[
            handoff(
                delegate,
                input_filter=lambda data: data.clone(
                    new_items=tuple(
                        item for item in data.new_items if isinstance(item, MessageOutputItem)
                    )
                ),
            )
        ],
    )
    failed = Runner.run_streamed(
        triage,
        "hello",
        session=session,
        run_config=RunConfig(
            tracing_disabled=True, session_input_callback=lambda _history, new: new
        ),
    )
    with pytest.raises(RuntimeError, match="session append failed"):
        async for _ in failed.stream_events():
            pass
    payload = json.loads(failed.to_state().to_string())
    assert "compaction_model_exchange" in payload["pending_session_write"]
    state = await RunState.from_json(triage, payload)
    resumed = await _run_session_resume(triage, state, session, False)
    assert resumed.final_output == "done"
    assert len(model.calls) == 2
    assert all(hidden not in call.input for call in model.calls)
    assert compact_calls == []
    assert (await session.get_items())[0] == hidden


class _TerminalLifecycleHooks(RunHooks[Any]):
    """Count the agent lifecycle hooks an application can attach its own effects to."""

    def __init__(self) -> None:
        self.starts = 0
        self.ends: list[str] = []

    async def on_agent_start(self, context: Any, agent: Agent[Any]) -> None:
        self.starts += 1

    async def on_agent_end(self, context: Any, agent: Agent[Any], output: Any) -> None:
        self.ends.append(str(output))


async def _terminal_output_session_state(
    streamed: bool,
    session: Session | None = None,
    hooks: RunHooks[Any] | None = None,
):
    """Pause on an approval whose tool output becomes the terminal agent output."""
    effects: list[int] = []

    @tool(needs_approval=True)
    async def charge(amount: int) -> str:
        effects.append(amount)
        return "receipt-7"

    model = ScriptedModel(
        [
            [get_function_tool_call("charge", '{"amount":7}', call_id="charge-1")],
            [get_text_message("retry-final")],
        ]
    )
    agent = Agent(
        name="payment",
        model=model,
        tools=[charge],
        tool_use_behavior="stop_on_first_tool",
    )
    session = session if session is not None else _FailingResumeSession()
    paused = await _run_session_resume(agent, "charge 7", session, streamed, hooks=hooks)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    return agent, model, session, state, effects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_streamed,retry_streamed", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_terminal_session_append_failure_rejects_every_later_resume(
    failing_streamed: bool, retry_streamed: bool, round_trip: bool, failure: str
) -> None:
    """An accepted terminal output whose append failed is not resumable, and never replayed."""
    hooks = _TerminalLifecycleHooks()
    agent, model, session, state, effects = await _terminal_output_session_state(
        failing_streamed, hooks=hooks
    )
    session.failure = failure
    with pytest.raises(RuntimeError) as error:
        await _run_session_resume(agent, state, session, failing_streamed, hooks=hooks)
    assert error.value is session.error

    # The output, its guardrails, and its terminal hooks all completed exactly once.
    assert effects == [7]
    assert len(model.calls) == 1
    assert hooks.ends == ["receipt-7"]
    starts_after_failure = hooks.starts
    assert state.to_json()["terminal_unrecoverable"] is True

    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    # Every later resume fails closed, including a second one.
    for _ in range(2):
        with pytest.raises(UserError, match="cannot be resumed"):
            await _run_session_resume(agent, state, session, retry_streamed, hooks=hooks)
        assert len(model.calls) == 1
        assert effects == [7]
        assert hooks.starts == starts_after_failure
        assert hooks.ends == ["receipt-7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_unrecoverable_terminal_state_rejects_before_any_resumed_work(
    streamed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection precedes Session reconciliation and sandbox preparation."""
    agent, model, session, state, effects = await _terminal_output_session_state(streamed)
    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run_session_resume(agent, state, session, streamed)

    async def _fail_get_items(*args: Any, **kwargs: Any) -> list[TResponseInputItem]:
        raise AssertionError("Session reconciliation must not run for a rejected terminal state")

    async def _fail_prepare_agent(*args: Any, **kwargs: Any):
        raise AssertionError("sandbox preparation must not run for a rejected terminal state")

    monkeypatch.setattr(type(session), "get_items", _fail_get_items)
    monkeypatch.setattr(SandboxRuntime, "prepare_agent", _fail_prepare_agent)

    restored = await RunState.from_json(agent, state.to_json())
    with pytest.raises(UserError, match="cannot be resumed"):
        await _run_session_resume(agent, restored, session, not streamed)
    assert len(model.calls) == 1
    assert effects == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_terminal_marker_is_cleared_once_the_turn_is_persisted(streamed: bool) -> None:
    """A terminal turn that persists cleanly leaves a normal, unmarked result."""
    agent, model, session, state, effects = await _terminal_output_session_state(streamed)
    result = await _run_session_resume(agent, state, session, streamed)

    assert result.final_output == "receipt-7"
    assert effects == [7]
    assert "terminal_unrecoverable" not in result.to_state().to_json()
    assert _charge_pair(await session.get_items()) == ["function_call", "function_call_output"]


@pytest.mark.asyncio
async def test_terminal_marker_rejects_an_older_schema_label() -> None:
    """The marker is only honored on the schema boundary that introduced it."""
    agent, _, session, state, _ = await _terminal_output_session_state(False)
    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run_session_resume(agent, state, session, False)

    payload = state.to_json()
    for entry in payload["context"].pop("function_tool_approvals", []):
        payload["context"]["approvals"][entry["tool_key"]] = entry["decision"]
    payload["$schemaVersion"] = "1.16"
    with pytest.raises(UserError, match="terminal marker is invalid"):
        await RunState.from_json(agent, payload)


@pytest.mark.asyncio
async def test_failed_stream_result_checkpoint_keeps_the_terminal_marker() -> None:
    """A checkpoint taken from a failed streamed run stays closed to resumes.

    A streamed result exists before its terminal append does, so `to_state()` is reachable on the
    failed attempt. If that snapshot dropped the marker it would look like an ordinary resumable
    state and bypass the rejection entirely.
    """
    agent, model, session, state, effects = await _terminal_output_session_state(True)
    session.failure = "before"
    streamed = Runner.run_streamed(
        agent, state, session=session, run_config=RunConfig(tracing_disabled=True)
    )
    with pytest.raises(RuntimeError, match="session append failed"):
        async for _ in streamed.stream_events():
            pass

    checkpoint = streamed.to_state()
    assert checkpoint.to_json()["terminal_unrecoverable"] is True

    restored = await RunState.from_json(agent, checkpoint.to_json())
    for candidate in (checkpoint, restored):
        with pytest.raises(UserError, match="cannot be resumed"):
            await _run_session_resume(agent, candidate, session, False)
    assert len(model.calls) == 1
    assert effects == [7]


@pytest.mark.asyncio
async def test_max_turns_handler_output_is_marked_before_it_is_persisted() -> None:
    """The max-turns fallback is a final output too, so its failed append closes the state."""
    handler_calls: list[str] = []
    hooks = _TerminalLifecycleHooks()

    @tool(needs_approval=True)
    async def charge(amount: int) -> str:
        return "receipt-7"

    model = ScriptedModel(
        [
            [get_function_tool_call("charge", '{"amount":7}', call_id="charge-1")],
            [get_text_message("unused")],
        ]
    )
    agent = Agent(name="payment", model=model, tools=[charge])
    session = _FailingResumeSession()
    config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(
        agent, "charge 7", session=session, run_config=config, max_turns=1, hooks=hooks
    )
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    def _handler(_data: Any) -> str:
        handler_calls.append("handled")
        return "max-turns-output"

    session.fail_on_output = "max-turns-output"
    with pytest.raises(RuntimeError, match="session append failed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=config,
            hooks=hooks,
            error_handlers={"max_turns": _handler},
        )

    # The handler and its end hook each ran exactly once before the append failed.
    assert handler_calls == ["handled"]
    assert hooks.ends == ["max-turns-output"]
    assert state.to_json()["terminal_unrecoverable"] is True

    with pytest.raises(UserError, match="cannot be resumed"):
        await Runner.run(
            agent,
            state,
            session=session,
            run_config=config,
            hooks=hooks,
            error_handlers={"max_turns": _handler},
        )
    assert handler_calls == ["handled"]
    assert hooks.ends == ["max-turns-output"]


@pytest.mark.asyncio
async def test_max_turns_guardrail_failure_leaves_the_state_retryable() -> None:
    """A handler output that never passed its guardrails must not close the state.

    `finalize_max_turns_handler_output()` drives the same save callback from its guardrail-error
    path. Marking there would reject every later resume for an output the caller never received,
    which is a worse outcome than the replay the marker exists to prevent.
    """
    guardrail_calls: list[str] = []

    @tool(needs_approval=True)
    async def charge(amount: int) -> str:
        return "receipt-7"

    @output_guardrail
    async def exploding(context: Any, agent: Agent[Any], output: Any) -> GuardrailFunctionOutput:
        guardrail_calls.append(str(output))
        raise RuntimeError("guardrail exploded")

    model = ScriptedModel([[get_function_tool_call("charge", '{"amount":7}', call_id="charge-1")]])
    agent = Agent(name="payment", model=model, tools=[charge], output_guardrails=[exploding])
    session = _FailingResumeSession()
    config = RunConfig(tracing_disabled=True)

    paused = await Runner.run(agent, "charge 7", session=session, run_config=config, max_turns=1)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])

    handlers: dict[str, Any] = {"max_turns": lambda _data: "max-turns-output"}
    session.fail_on_output = "max-turns-output"
    with pytest.raises(RuntimeError, match="session append failed"):
        await Runner.run(agent, state, session=session, run_config=config, error_handlers=handlers)

    assert guardrail_calls == ["max-turns-output"]
    assert "terminal_unrecoverable" not in state.to_json()

    # The retry reports the real guardrail failure rather than a fail-closed rejection.
    with pytest.raises(RuntimeError, match="guardrail exploded"):
        await Runner.run(agent, state, session=session, run_config=config, error_handlers=handlers)
