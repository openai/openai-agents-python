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
from tests.memory.test_resumable_session import MemoryStore, run_agent
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
        "preconditions": {"version": 1, "first_approval": True},
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
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("approval_after_count", [False, True], ids=["before-count", "after-count"])
async def test_native_pause_preserves_all_sampling_usage_across_session_resume(
    tmp_path, monkeypatch, threads, streamed, approval_after_count
):
    from tests.models.test_codex_pending_approval import ApprovalThread, approval, raw
    from tests.models.test_codex_provider import native_pair

    earlier = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": 1,
        "reasoning_output_tokens": 1,
    }
    current = {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 3,
        "reasoning_output_tokens": 2,
    }
    cumulative = {key: earlier[key] + current[key] for key in earlier}

    # The wire double preserves the completion/count ordering around a real Runner pause.
    class SamplingApprovalThread(ApprovalThread):
        async def events(self):
            yield {
                "type": "raw_response_completed",
                "response_id": "earlier",
                "token_usage": earlier,
            }
            yield {"type": "token_count", "info": {"total_token_usage": earlier}}
            call, _ = native_pair(call_id="pending")
            yield raw(call)
            if not approval_after_count:
                yield approval()
            yield {
                "type": "raw_response_completed",
                "response_id": "current",
                "token_usage": current,
            }
            yield {"type": "token_count", "info": {"total_token_usage": cumulative}}
            if approval_after_count:
                yield approval()

    def tokens(usage):
        return (
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.input_tokens_details.cached_tokens,
            usage.input_tokens_details.cache_write_tokens,
            usage.output_tokens_details.reasoning_tokens,
        )

    model = CodexProvider(cwd=tmp_path).get_model("fixture-model")
    agent = Agent(name="worker", model=model)
    first = SamplingApprovalThread()
    threads[0].extend([first, FakeThread(text="finished")])
    calls = []

    async def execute(checkpoint, *, tools, tracing):
        calls.append(checkpoint["call"]["call_id"])
        return "native accepted"

    monkeypatch.setattr(model, "_run_approved_native_call", execute)
    store = MemoryStore()
    session = await ResumableSession.create(store)
    paused = await run_agent(agent, "start", session, streamed)
    assert first.recover_approvals
    assert first.closed and len(paused.interruptions) == 1 and calls == []
    assert tokens(paused.context_wrapper.usage) == (15, 5, 20, 3, 4, 3)
    assert store.checkpoint(session.session_id)["status"] == "paused"

    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    state.approve(state.get_interruptions()[0])
    completed = await run_agent(agent, state, None, streamed)
    assert completed.final_output == "finished" and calls == ["pending"]
    # The fresh native thread adds its own 10 input and 7 output tokens exactly once.
    assert tokens(completed.context_wrapper.usage) == (25, 12, 37, 6, 4, 5)


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
@pytest.mark.parametrize("include_data,cancel", [(False, False), (True, False), (False, True)])
async def test_restored_native_execution_observes_live_start_and_interruption(
    tmp_path, monkeypatch, threads, include_data, cancel
):
    from agents.tracing import get_current_span
    from tests.testing_processor import SPAN_PROCESSOR_TESTING

    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    updates = asyncio.Event()
    events = []
    calls = []
    original_start = SPAN_PROCESSOR_TESTING.on_span_start
    caller_spans = []

    def on_start(span):
        original_start(span)
        if span.span_data.type == "function":
            caller_spans.append(get_current_span())
            started.set()

    monkeypatch.setattr(SPAN_PROCESSOR_TESTING, "on_span_start", on_start)

    # The FFI double preserves suspension and cancellation at the native execution boundary.
    class RestoredThread(FakeThread):
        def enable_tracing(self, mode="metadata"):
            calls.append(mode)

        async def wait_for_operations(self):
            await updates.wait()

        def drain_operations(self):
            encoded = json.dumps(events)
            events.clear()
            updates.clear()
            return encoded

        async def resolve_approval(self, call_json, request, preconditions):
            key = {"kind": "tool", "name": "exec_command", "id": json.loads(call_json)["call_id"]}
            events.append(dict(key, phase="start", at=1_795_154_400_000))
            if calls == ["content"]:
                events.append(dict(key, phase="input", input="private command"))
            updates.set()
            await release.wait()
            events.append(dict(key, phase="end", outcome="completed", at=1_795_154_402_000))
            if calls == ["content"]:
                events.append(dict(key, phase="output", output="native accepted"))
            updates.set()
            return "native accepted"

        async def close(self):
            await super().close()
            closed.set()

    agent, store, session, config, _, _, _ = await setup_native(tmp_path, monkeypatch)
    await Runner.run(agent, "start", session=session, run_config=config)
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    state.approve(state.get_interruptions()[0])
    monkeypatch.delattr(agent.model, "_run_approved_native_call")
    threads[0].append(RestoredThread())
    task = asyncio.create_task(
        Runner.run(
            agent,
            state,
            run_config=RunConfig(
                tracing={"operation_detail": "content"},
                trace_include_sensitive_data=include_data,
            ),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done()
        assert calls == ["content" if include_data else "metadata"]
        assert caller_spans[0] is not None
        assert caller_spans[0].span_data.type == "agent"
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            result = await asyncio.wait_for(task, 2)
            assert result.final_output == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()
    (span,) = [
        span
        for span in fetch_ordered_spans()
        if span.span_data.type == "function"
        and span.span_data.operation.tool_call_id == "native-call"
    ]
    assert span.span_data.operation.outcome == ("interrupted" if cancel else "completed")
    assert span.started_at == "2026-11-20T06:00:00+00:00"
    assert ("private command" in repr(span.span_data.input)) == include_data
    assert span.parent_id == caller_spans[0].span_id
