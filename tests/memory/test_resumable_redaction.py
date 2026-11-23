"""Trusted redaction policy and checkpoint refusal across worker restarts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.exceptions import UserError
from agents.memory.resumable_session import ResumableSession
from agents.testing import ScriptedModel
from tests.memory.test_resumable_session import MemoryStore
from tests.test_responses import get_function_tool_call, get_text_message

SECRET = "synthetic-session-private-value"


def redact(value: str) -> str:
    return value.replace(SECRET, "[redacted]")


async def run(agent: Agent[Any], input: Any, *, streamed: bool, **kwargs: Any) -> Any:
    if not streamed:
        return await Runner.run(agent, input, **kwargs)
    result = Runner.run_streamed(agent, input, **kwargs)
    async for _ in result.stream_events():
        pass
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("paused", [False, True], ids=["ready", "paused"])
async def test_protected_session_requires_policy_before_claim_after_restart(
    streamed: bool, paused: bool
) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)

    @tool(needs_approval=True)
    def change() -> str:
        return "changed"

    model = ScriptedModel(
        [[get_function_tool_call("change", "{}")]] if paused else [[get_text_message("answer")]]
    )
    agent = Agent(name="assistant", model=model, tools=[change])
    await run(
        agent,
        "question",
        streamed=streamed,
        session=session,
        run_config=RunConfig(tracing_disabled=True, redact_text=redact),
    )
    saved = store.records[session.session_id]
    assert json.loads(saved)["version"] == 2
    restored = await ResumableSession.resume(store, session.session_id)
    input: Any = "follow-up"
    if paused:
        input = await restored.load_state(agent)
        input.approve(input.get_interruptions()[0])
    with pytest.raises(UserError, match="RunConfig.redact_text"):
        await run(
            agent,
            input,
            streamed=streamed,
            session=restored,
            run_config=RunConfig(tracing_disabled=True),
        )
    assert store.records[session.session_id] == saved
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_protected_run_refuses_existing_dirty_history_before_claim() -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    agent = Agent(name="assistant", model=ScriptedModel([[get_text_message("answer")]]))
    await Runner.run(agent, SECRET, session=session, run_config=RunConfig(tracing_disabled=True))
    saved = store.records[session.session_id]
    assert json.loads(saved)["version"] == 1
    assert SECRET in saved
    restored = await ResumableSession.resume(store, session.session_id)
    with pytest.raises(UserError) as failure:
        await Runner.run(
            agent,
            "follow-up",
            session=restored,
            run_config=RunConfig(tracing_disabled=True, redact_text=redact),
        )
    assert SECRET not in str(failure.value)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert store.records[session.session_id] == saved


@pytest.mark.asyncio
async def test_dirty_context_is_not_written_in_pending_checkpoint() -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)

    @tool(needs_approval=True)
    def change() -> str:
        return "changed"

    agent = Agent(
        name="assistant",
        model=ScriptedModel([[get_function_tool_call("change", "{}")]]),
        tools=[change],
    )
    with pytest.raises(UserError) as failure:
        await Runner.run(
            agent,
            "question",
            session=session,
            context={"private": SECRET},
            run_config=RunConfig(tracing_disabled=True, redact_text=redact),
        )
    assert SECRET not in str(failure.value)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert all(SECRET not in value for value in store.records.values())
    assert json.loads(store.records[session.session_id])["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("override", ["load_state", "runner"])
async def test_resumed_dirty_context_is_refused_before_claim(streamed: bool, override: str) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    calls = []

    @tool(needs_approval=True)
    def change() -> str:
        calls.append(True)
        return "changed"

    agent = Agent(
        name="assistant",
        model=ScriptedModel([[get_function_tool_call("change", "{}")]]),
        tools=[change],
    )
    config = RunConfig(tracing_disabled=True, redact_text=redact)
    await Runner.run(
        agent, "question", session=session, context={"private": "safe"}, run_config=config
    )
    saved = store.records[session.session_id]
    restored = await ResumableSession.resume(store, session.session_id)
    context = {"private": SECRET}
    if override == "load_state":
        state = await restored.load_state(agent, context_override=context)
    else:
        state = await restored.load_state(agent)
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError) as failure:
        await run(
            agent,
            state,
            streamed=streamed,
            run_config=config,
            context=context if override == "runner" else None,
        )
    assert SECRET not in str(failure.value)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert store.records[session.session_id] == saved
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_standalone_approval_retains_redaction_requirement(streamed: bool) -> None:
    from agents import RunState
    from agents.run_state import CURRENT_SCHEMA_VERSION

    executed: list[str] = []

    @tool(needs_approval=True)
    def change(value: str) -> str:
        executed.append(value)
        return SECRET

    model = ScriptedModel(
        [
            [get_function_tool_call("change", json.dumps({"value": SECRET}))],
            [get_text_message("useful " + SECRET)],
        ]
    )
    agent = Agent(name="assistant", model=model, tools=[change])
    config = RunConfig(tracing_disabled=True, redact_text=redact)
    paused = await run(agent, SECRET, streamed=streamed, run_config=config)
    # Serialize after the Runner's policy context has exited, retaining only its requirement.
    snapshot = paused.to_state().to_json()
    assert snapshot["$schemaVersion"] == CURRENT_SCHEMA_VERSION
    assert snapshot["requires_redaction"] is True
    assert SECRET not in json.dumps(snapshot)
    restored = await RunState.from_string(agent, json.dumps(snapshot))
    restored.approve(restored.get_interruptions()[0])
    with pytest.raises(UserError, match="RunConfig.redact_text"):
        await run(agent, restored, streamed=streamed, run_config=RunConfig(tracing_disabled=True))
    assert executed == []
    assert len(model.calls) == 1
    completed = await run(agent, restored, streamed=streamed, run_config=config)
    assert executed == ["[redacted]"]
    assert completed.final_output == "useful [redacted]"
    assert completed.to_state().to_json()["requires_redaction"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_standalone_serialization_checks_context_at_every_export(streamed: bool) -> None:
    from agents import RunState

    context = {"value": SECRET}
    agent = Agent(name="worker", model=ScriptedModel([[get_text_message("safe")]]))
    result = await run(
        agent, "go", streamed=streamed, context=context, run_config=RunConfig(redact_text=redact)
    )
    state = result.to_state()
    with pytest.raises(UserError, match="redact_text"):
        state.to_json()
    state._context.context["value"] = "safe"
    clean = state.to_string()
    assert SECRET not in clean
    state._context.context["value"] = SECRET
    with pytest.raises(UserError, match="redact_text"):
        state.to_string()
    restored = await RunState.from_string(agent, clean)
    with pytest.raises(UserError, match="redact_text"):
        restored.to_json()


@pytest.mark.asyncio
async def test_standalone_dirty_legacy_state_is_refused_before_execution() -> None:
    from agents import RunState

    executed: list[str] = []

    @tool(needs_approval=True)
    def change(value: str) -> str:
        executed.append(value)
        return "changed"

    model = ScriptedModel([[get_function_tool_call("change", json.dumps({"value": SECRET}))]])
    agent = Agent(name="assistant", model=model, tools=[change])
    paused = await Runner.run(agent, SECRET, run_config=RunConfig(tracing_disabled=True))
    snapshot = paused.to_state().to_json()
    snapshot["$schemaVersion"] = "1.23"
    snapshot.pop("requires_redaction", None)
    restored = await RunState.from_json(agent, snapshot)
    restored.approve(restored.get_interruptions()[0])
    with pytest.raises(UserError) as failure:
        await Runner.run(
            agent, restored, run_config=RunConfig(tracing_disabled=True, redact_text=redact)
        )
    assert SECRET not in str(failure.value)
    assert executed == []
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "marker"), [("1.23", True), ("1.24", False), ("1.24", "true"), ("1.24", 1)]
)
async def test_standalone_redaction_marker_rejects_invalid_labels(
    version: str, marker: Any
) -> None:
    from agents import RunState
    from agents.run_context import RunContextWrapper

    agent = Agent(name="assistant")
    snapshot = RunState(RunContextWrapper(None), "question", agent).to_json()
    snapshot["$schemaVersion"] = version
    snapshot["requires_redaction"] = marker
    with pytest.raises(UserError, match="redaction marker"):
        await RunState.from_json(agent, snapshot)
