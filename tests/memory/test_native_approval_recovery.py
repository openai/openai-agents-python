from __future__ import annotations

import asyncio
import copy
import json

import pytest

from agents import Agent, ResumableSession, RunConfig, Runner, RunState
from agents.exceptions import AgentsException, UserError
from agents.items import ModelResponse
from agents.memory.resumable_session import ResumableSessionError
from agents.models._codex_items import CodexPendingCustomCall, CodexPendingFunctionCall
from agents.models.codex import CodexProvider
from agents.testing import ScriptedModel
from agents.usage import Usage
from tests.memory.test_resumable_session import MemoryStore
from tests.models.test_codex_provider import FakeThread, threads as threads
from tests.test_responses import get_text_message
from tests.testing_processor import fetch_ordered_spans


def pending_call(tmp_path, kind):
    call = {
        "type": "function_call" if kind == "exec_command" else "custom_tool_call",
        "id": "native-item",
        "call_id": "native-call",
        "name": kind,
        "status": "in_progress",
    }
    if kind == "exec_command":
        call["arguments"] = '{"cmd":"echo harmless"}'
        item_type = CodexPendingFunctionCall
    else:
        call["input"] = "*** Begin Patch\n*** Add File: harmless.txt\n+safe\n*** End Patch\n"
        item_type = CodexPendingCustomCall
    checkpoint = {
        "version": 1,
        "cwd": str(tmp_path.resolve()),
        "call": copy.deepcopy(call),
        "request": {"type": "native_approval", "call_id": "native-call"},
        "model": "fixture-model",
    }
    item = item_type(**call, checkpoint=checkpoint)
    return item.model_copy(update={"codex_reasoning_summary": "Review this effect."})


async def setup_native(tmp_path, monkeypatch, kind="exec_command"):
    provider = CodexProvider(cwd=tmp_path)
    model = provider.get_model("fixture-model")
    pending = pending_call(tmp_path, kind)
    calls = []
    requests = []

    async def respond(*args, **kwargs):
        requests.append(kwargs.get("input", args[1] if len(args) > 1 else None))
        output = [pending] if len(requests) == 1 else [get_text_message("finished")]
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    async def execute(checkpoint, *, tools, tracing):
        calls.append(checkpoint)
        return "native accepted"

    monkeypatch.setattr(model, "get_response", respond)
    monkeypatch.setattr(model, "_run_approved_native_call", execute, raising=False)
    agent = Agent(name="worker", model=model)
    store = MemoryStore()
    session = await ResumableSession.create(store)
    config = RunConfig(tracing_disabled=True)
    return agent, store, session, config, pending, calls, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
@pytest.mark.parametrize("approved", [True, False])
async def test_native_approval_round_trip_uses_original_runtime_only(
    tmp_path, monkeypatch, kind, approved
):
    agent, store, session, config, pending, calls, requests = await setup_native(
        tmp_path, monkeypatch, kind
    )
    first = await Runner.run(agent, "do the native operation", session=session, run_config=config)
    assert len(first.interruptions) == 1
    assert calls == []
    assert store.checkpoint(session.session_id)["status"] == "paused"
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    interruption = state.get_interruptions()[0]
    assert interruption.arguments == (
        pending.arguments if kind == "exec_command" else pending.input
    )
    if approved:
        state.approve(interruption)
    else:
        state.reject(interruption, rejection_message="Leave the release branch alone.")
    result = await Runner.run(agent, state, session=recovered, run_config=config)
    assert result.final_output == "finished"
    assert calls == ([pending.checkpoint] if approved else [])
    assert "checkpoint" not in repr(requests[-1])
    expected_message = "native accepted" if approved else "Leave the release branch alone."
    assert any(
        item.get("call_id") == interruption.call_id and item.get("output") == expected_message
        for item in requests[-1]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_plain_runner_rejects_unrecoverable_native_pause(tmp_path, monkeypatch, streamed):
    agent, _, _, config, pending, calls, _ = await setup_native(tmp_path, monkeypatch)
    if streamed:
        scripted = ScriptedModel([[pending]])
        monkeypatch.setattr(agent.model, "stream_response", scripted.stream_response)
    with pytest.raises(UserError, match="session=ResumableSession"):
        if streamed:
            result = Runner.run_streamed(agent, "start", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "start", run_config=config)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
@pytest.mark.parametrize("field", ["name", "call_id", "id", "payload"])
async def test_display_mutation_cannot_be_approved(tmp_path, monkeypatch, kind, field):
    agent, store, session, config, _, calls, _ = await setup_native(tmp_path, monkeypatch, kind)
    await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    item = state.get_interruptions()[0]
    field = ("arguments" if kind == "exec_command" else "input") if field == "payload" else field
    setattr(item.raw_item, field, "changed")
    with pytest.raises(UserError):
        state.approve(item)
    assert not calls
    assert store.checkpoint(session.session_id)["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["payload", "cwd", "request", "model"])
async def test_coordinated_receipt_mutation_fails_before_claim(tmp_path, monkeypatch, field):
    agent, store, session, config, _, calls, _ = await setup_native(tmp_path, monkeypatch)
    await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    state.approve(state.get_interruptions()[0])
    # Rewrite every serialized copy consistently; the store's original still binds authority.
    snapshot = state.to_json()

    def rewrite(value):
        if isinstance(value, dict):
            if field == "payload" and value.get("type") == "function_call":
                value["arguments"] = '{"cmd":"changed"}'
            if value.get("version") == 1 and "cwd" in value and "request" in value:
                if field == "cwd":
                    value["cwd"] = str(tmp_path / "other")
                elif field == "request":
                    value["request"] = {"type": "changed"}
                elif field == "model":
                    value["model"] = "changed"
            for child in value.values():
                rewrite(child)
        elif isinstance(value, list):
            for child in value:
                rewrite(child)

    rewrite(snapshot)
    changed = await RunState.from_string(agent, json.dumps(snapshot))
    changed._resumable_binding = state._resumable_binding
    original = store.records[session.session_id]
    with pytest.raises(ResumableSessionError):
        await Runner.run(agent, changed, session=recovered, run_config=config)
    assert store.records[session.session_id] == original
    assert not calls


@pytest.mark.asyncio
async def test_no_decision_repauses_and_unbound_copy_never_dispatches(tmp_path, monkeypatch):
    agent, store, session, config, _, calls, _ = await setup_native(tmp_path, monkeypatch)
    first = await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    result = await Runner.run(agent, state, session=recovered, run_config=config)
    assert len(result.interruptions) == 1
    assert not calls
    copied = await RunState.from_string(agent, first.to_state().to_string())
    copied.approve(copied.get_interruptions()[0])
    with pytest.raises(UserError):
        await Runner.run(agent, copied, run_config=config)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
async def test_completed_native_call_id_cannot_reuse_a_previous_decision(
    tmp_path, monkeypatch, kind
):
    agent, store, session, config, pending, calls, requests = await setup_native(
        tmp_path, monkeypatch, kind
    )

    async def repeat_native_call(*args, **kwargs):
        requests.append(kwargs.get("input", args[1] if len(args) > 1 else None))
        if len(requests) <= 2:
            # The adapter can attest a new native request with a repeated model call ID.
            repeated = pending.model_copy(deep=True)
            repeated.checkpoint["request"]["turn_id"] = f"native-turn-{len(requests)}"
            output = [repeated]
        else:
            output = [get_text_message("finished")]
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    monkeypatch.setattr(agent.model, "get_response", repeat_native_call)
    await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    state.approve(state.get_interruptions()[0])

    # Native execution stays at the adapter seam while public resume owns approval state.
    with pytest.raises(UserError, match="completed"):
        repeated_result = await Runner.run(agent, state, session=recovered, run_config=config)
        assert len(repeated_result.interruptions) == 1
        assert len(calls) == 1
        replacement = await ResumableSession.resume(store, session.session_id)
        repeated_state = await replacement.load_state(agent)
        await Runner.run(agent, repeated_state, session=replacement, run_config=config)
        assert len(calls) == 1, "The previous decision authorized another native dispatch."

    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_sticky_native_decisions_are_rejected(tmp_path, monkeypatch, approved):
    agent, store, session, config, _, calls, _ = await setup_native(tmp_path, monkeypatch)
    await Runner.run(agent, "start", session=session, run_config=config)
    state = await session.load_state(agent)
    item = state.get_interruptions()[0]
    with pytest.raises(UserError, match="original call only"):
        if approved:
            state.approve(item, always_approve=True)
        else:
            state.reject(item, always_reject=True)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_native_pause_waits_for_checkpoint_ack(tmp_path, monkeypatch, streamed):
    agent, store, session, config, pending, calls, _ = await setup_native(tmp_path, monkeypatch)
    store.block_checkpoint = True
    events = []
    if streamed:
        scripted = ScriptedModel([[pending]])
        monkeypatch.setattr(agent.model, "stream_response", scripted.stream_response)
        result = Runner.run_streamed(agent, "start", session=session, run_config=config)

        async def consume():
            async for event in result.stream_events():
                events.append(event)
            return result

        task = asyncio.create_task(consume())
    else:
        task = asyncio.create_task(Runner.run(agent, "start", session=session, run_config=config))
    try:
        await asyncio.wait_for(store.saving.wait(), 5)
        assert not task.done()
        assert store.checkpoint(session.session_id)["status"] == "active"
        assert not calls
        if streamed:
            assert not result.interruptions
            assert not any(
                getattr(event, "name", None) == "tool_approval_requested" for event in events
            )
            with pytest.raises(AgentsException):
                result.to_state()
    finally:
        store.release.set()
        finished = await asyncio.wait_for(task, 5)
    assert len(finished.interruptions) == 1
    assert store.checkpoint(session.session_id)["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_data", [False, True])
async def test_resume_exports_the_original_native_operation_under_current_trace_policy(
    tmp_path, monkeypatch, threads, include_data
):
    class RestoredThread(FakeThread):
        async def resolve_approval(self, call_json, request, preconditions, capture):
            assert capture == ("content" if include_data else "metadata")
            operation = {
                "threadId": self.thread_id,
                "turnId": "replacement-turn",
                "operationId": "from-native-dispatch",
                "kind": "tool",
                "toolName": "exec_command",
                "callId": json.loads(call_json)["call_id"],
                "startedAt": "2026-11-20T06:00:00.000Z",
                "input": {"value": "private commit message"},
            }
            completed = dict(operation, completedAt="2026-11-20T06:00:02.000Z", outcome="success")
            events = [
                json.dumps({"method": f"turn/operation/{kind}", "params": params})
                for kind, params in [("started", operation), ("completed", completed)]
            ]
            return "native accepted", "replacement-turn", events, None

    agent, store, session, config, _, _, _ = await setup_native(tmp_path, monkeypatch)
    await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    pending = state.get_interruptions()[0]
    state.approve(pending)
    monkeypatch.delattr(agent.model, "_run_approved_native_call")
    threads[0].append(RestoredThread())
    result = await Runner.run(
        agent,
        state,
        run_config=RunConfig(
            tracing={"operation_detail": "content"}, trace_include_sensitive_data=include_data
        ),
    )
    assert result.final_output == "finished"
    (operation_span,) = [
        span.span_data
        for span in fetch_ordered_spans()
        if span.span_data.type == "function"
        and span.span_data.operation.operation_id == "from-native-dispatch"
    ]
    assert operation_span.operation.outcome == "completed"
    assert operation_span.operation.tool_call_id == pending.call_id
    assert ("private commit message" in repr(operation_span.input)) == include_data
