from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from openai.types.responses.response_output_item import McpApprovalRequest

from agents import (
    Agent,
    HostedMCPTool,
    RunState,
    StopAtTools,
    ToolsToFinalOutputResult,
    function_tool,
)
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _DEFERRING_BEHAVIOR,
    _EXPECTED_PAIR,
    _call_ids,
    _FailingResumeSession,
    _make_deferring_agent,
    _make_terminal_tool_agent,
    _orphaned_outputs,
    _parked_and_approved,
    _parked_pair,
    _RecordingConversationsSession,
    _run,
    always_fine,
    look_up,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_streamed", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
@pytest.mark.parametrize("failure", ["before", "after"], ids=["atomic-failure", "lost-ack"])
async def test_a_failed_settle_of_the_held_batch_is_recovered_on_the_next_resume(
    retry_streamed: bool, streamed: bool, round_trip: bool, failure: str
) -> None:
    session = _FailingResumeSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    session.failure = failure
    with pytest.raises(RuntimeError) as error:
        await _run(agent, state, session, streamed=streamed)
    assert error.value is session.error
    if round_trip:
        state = await RunState.from_json(agent, state.to_json())

    result = await _run(agent, state, session, streamed=retry_streamed)
    assert result.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR
    assert "pending_session_write" not in result.to_state().to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_failed_final_settle_fails_closed_with_the_batch_recorded(
    streamed: bool,
) -> None:
    # The final-output settle registers the claimed batch before appending, so a crash
    # inside that append leaves the batch recorded on the state instead of silently
    # losing the only copy of the approved call and its output. The resulting
    # checkpoint is rejected on load on purpose: the run ended mid-settle, and failing
    # closed beats replaying an approved side effect as if nothing happened.
    session = _FailingResumeSession()
    # A guardrail-less resume: the final sweep returns the resolved items verbatim, so
    # the held batch itself rides the append that fails.
    resume_agent = _make_terminal_tool_agent(with_guardrails=False)
    state = await _parked_and_approved(
        _make_terminal_tool_agent(), session, streamed=streamed, resume_agent=resume_agent
    )

    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run(resume_agent, state, session, streamed=streamed)

    pending = state._pending_session_write
    assert pending is not None
    recorded = {item.get("call_id") for item in pending["items"]}
    assert "call_PARKED" in recorded
    with pytest.raises(Exception, match="pending Session write"):
        await RunState.from_json(resume_agent, state.to_json())


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_failed_guarded_final_settle_fails_closed(streamed: bool) -> None:
    # With output guardrails the final sweep rebuilds the response and the held batch
    # is deduplicated out of the append, but the append still lands the approved call
    # and output, so the recovery registration must stay armed: a crash inside it must
    # leave the batch recorded, not silently lost. Guards the interaction between the
    # dedup and the crash-safe registration.
    session = _FailingResumeSession()
    resume_agent = _make_terminal_tool_agent(with_preamble=True)
    state = await _parked_and_approved(
        _make_terminal_tool_agent(with_preamble=True),
        session,
        streamed=streamed,
        resume_agent=resume_agent,
    )

    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await _run(resume_agent, state, session, streamed=streamed)

    pending = state._pending_session_write
    assert pending is not None
    assert "call_PARKED" in {item.get("call_id") for item in pending["items"]}


@pytest.mark.asyncio
async def test_entry_settle_restores_the_conversations_sanitization() -> None:
    # A batch extended while detached missed the Conversations-specific sanitization;
    # the attached entry settle must restore it or the create-items request rejects
    # stale provider ids the normal persistence path strips.
    from agents.run_internal.run_steps import NextStepRunAgain
    from agents.run_internal.session_persistence import resume_pending_session_write

    session = _RecordingConversationsSession()
    state = RunState(
        context=None,
        original_input="go",
        starting_agent=_make_deferring_agent(),
        max_turns=5,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": "conv-1",
        "items": [
            {
                "type": "function_call",
                "call_id": "call_PARKED",
                "name": "write_thing",
                "arguments": "{}",
                "id": "__fake_id__",
            },
            {
                "type": "function_call_output",
                "call_id": "call_PARKED",
                "output": "wrote:x",
                "id": "__fake_id__",
            },
        ],
        "before": None,
        "persisted_count": 2,
        "held": True,
    }

    await resume_pending_session_write(state, session)  # type: ignore[arg-type]

    assert state._pending_session_write is None
    assert [item.get("call_id") for item in session.added] == ["call_PARKED", "call_PARKED"]
    assert all("id" not in item for item in session.added)


def _boom_extractor(ctx: Any) -> dict[str, Any]:
    raise RuntimeError("extractor boom")


@function_tool(needs_approval=True, custom_data_extractor=_boom_extractor)
async def read_secret_with_failing_extractor(query: str) -> str:
    return "SECRET-VALUE-42"


def _make_secret_failing_extractor_handoff_agent() -> Agent:
    """A secret-bearing gated tool whose extractor crashes, resolved into a filtered handoff."""
    from agents import handoff
    from agents.extensions.handoff_filters import remove_all_tools

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel([ModelStep(output=[assistant_message("done")])]),
    )
    return Agent(
        name="deferred repro (secret, failing extractor)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call(
                            "read_secret_with_failing_extractor",
                            {"query": "x"},
                            call_id="call_SECRET",
                        ),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[read_secret_with_failing_extractor],
        handoffs=[handoff(target, input_filter=remove_all_tools)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_the_filters_authority_survives_a_json_retry_of_the_crashed_turn(
    streamed: bool,
) -> None:
    # A post-output callback crash leaves the folded output on the checkpoint, and the
    # supported retry path serializes and reloads that state. The fold's ownership
    # rides the record with the turn it belongs to, so the reloaded retry's filter
    # keeps its authority over the turn it is re-running: the batch's copy is not
    # pairing evidence and the filtered secret stays out of the Session.
    session = SimpleListSession()
    agent = _make_secret_failing_extractor_handoff_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    with pytest.raises(Exception, match="extractor boom"):
        await _run(agent, state, session, streamed=streamed)

    reloaded = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))
    retry = await _run(agent, reloaded, session, streamed=streamed)
    assert retry.final_output == "done"

    items = await session.get_items()
    assert not any("SECRET-VALUE-42" in json.dumps(item) for item in items)
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"


def _boom_custom_data_extractor(ctx: Any) -> dict[str, Any]:
    raise RuntimeError("extractor boom")


@function_tool(
    name_override="write_thing",
    needs_approval=True,
    custom_data_extractor=_boom_custom_data_extractor,
)
def write_thing_with_failing_extractor(query: str) -> str:
    return f"wrote:{query}"


def _make_failing_extractor_agent() -> Agent:
    """The approved tool succeeds, then its post-output callback raises."""
    return Agent(
        name="deferred repro (failing extractor)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[look_up, write_thing_with_failing_extractor],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_post_output_callback_failure_keeps_the_executed_output(streamed: bool) -> None:
    # The approved tool ran and its output was committed when the post-output callback
    # raised. A retry skips the completed invocation and produces no new session items,
    # so the batch has to carry that output from the commit boundary or the executed
    # call and its result vanish from history.
    session = SimpleListSession()
    agent = _make_failing_extractor_agent()
    agent.tool_use_behavior = StopAtTools(stop_at_tool_names=["write_thing"])
    state = await _parked_and_approved(agent, session, streamed=streamed)

    with pytest.raises(Exception, match="extractor boom"):
        await _run(agent, state, session, streamed=streamed)

    pending = state._pending_session_write
    assert pending is not None
    assert _parked_pair(pending["items"]) == _EXPECTED_PAIR

    restored = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))
    agent.output_guardrails = []
    # The completed tool now supplies the final output without producing new items.
    # Its failing extractor would raise again if the retry executed the tool again.
    result = await _run(agent, restored, session, streamed=streamed)
    assert result.final_output == "wrote:x"
    assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR
    completed = await RunState.from_json(agent, result.to_state().to_json())
    assert completed._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_callback_failure_preserves_model_order_for_completed_outputs(streamed: bool) -> None:
    second_committed = asyncio.Event()
    executions: list[str] = []

    def mark_second_committed(ctx: Any) -> None:
        second_committed.set()

    @function_tool(needs_approval=True, custom_data_extractor=_boom_custom_data_extractor)
    async def first() -> str:
        await second_committed.wait()
        executions.append("first")
        return "first result"

    @function_tool(needs_approval=True, custom_data_extractor=mark_second_committed)
    async def second() -> str:
        executions.append("second")
        return "second result"

    agent = Agent(
        name="ordered callback recovery",
        tools=[first, second],
        output_guardrails=[always_fine],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["first"]),
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("first", {}, call_id="call_FIRST"),
                        function_call("second", {}, call_id="call_SECOND"),
                    ]
                )
            ]
        ),
    )
    session = SimpleListSession()
    parked = await _run(agent, "go", session, streamed=streamed)
    state = await RunState.from_json(agent, parked.to_state().to_json())
    for interruption in state.get_interruptions():
        state.approve(interruption)

    # The callback signals after the second output is committed, ensuring that
    # completion order differs from the model's call order before the failure.
    with pytest.raises(Exception, match="extractor boom"):
        await _run(agent, state, session, streamed=streamed)

    restored = await RunState.from_json(agent, json.loads(json.dumps(state.to_json())))
    agent.output_guardrails = []
    result = await _run(agent, restored, session, streamed=streamed)

    assert result.final_output == "first result"
    assert executions == ["second", "first"]
    assert [
        (item["call_id"], item["output"])
        for item in await session.get_items()
        if item.get("type") == "function_call_output"
    ] == [("call_FIRST", "first result"), ("call_SECOND", "second result")]
    completed = await RunState.from_json(agent, result.to_state().to_json())
    assert completed._pending_session_write is None


@pytest.mark.parametrize("streamed", [False, True])
async def test_checkpointed_mcp_approval_survives_callback_failure_and_json_retry(
    streamed: bool,
) -> None:
    executions = 0
    decisions = 0

    @function_tool(needs_approval=True)
    def write_once() -> str:
        nonlocal executions
        executions += 1
        return "written"

    def decide(context: Any, results: Any) -> ToolsToFinalOutputResult:
        nonlocal decisions
        decisions += 1
        if decisions == 1:
            raise RuntimeError("decision failed")
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

    request = McpApprovalRequest(
        id="mcpr_test",
        type="mcp_approval_request",
        server_label="srv",
        name="remote",
        arguments="{}",
    )
    agent = Agent(
        name="agent",
        tools=[
            write_once,
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "srv",
                    "server_url": "https://example.com",
                    "require_approval": "always",
                }
            ),
        ],
        output_guardrails=[always_fine],
        tool_use_behavior=decide,
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("write_once", {}, call_id="local_write"), request]),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
    )
    session = SimpleListSession()
    # ScriptedModel cannot synthesize MCP stream events; both public resume paths
    # consume the same serialized checkpoint from the ordinary initial run.
    parked = await _run(agent, "go", session, streamed=False)
    state = await RunState.from_json(agent, parked.to_state().to_json())
    for approval in state.get_interruptions():
        state.approve(approval)
    with pytest.raises(RuntimeError, match="decision failed"):
        await _run(agent, state, session, streamed=streamed)
    assert await session.get_items() == [{"role": "user", "content": "go"}]

    state = await RunState.from_json(agent, state.to_json())
    result = await _run(agent, state, session, streamed=streamed)
    assert result.final_output == "done"
    assert executions == 1
    history = await session.get_items()
    mcp_items = [
        item
        for item in history
        if item.get("type") in ("mcp_approval_request", "mcp_approval_response")
    ]
    assert [item["type"] for item in mcp_items] == [
        "mcp_approval_request",
        "mcp_approval_response",
    ]
    assert mcp_items[0]["id"] == "mcpr_test"
    assert mcp_items[1]["approval_request_id"] == "mcpr_test"
    assert mcp_items[1]["approve"] is True
    assert [item["type"] for item in history if item.get("call_id") == "local_write"] == [
        "function_call",
        "function_call_output",
    ]
