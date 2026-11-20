"""Caller-level checkpoint publication and fresh-handle recovery contracts."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from agents import Agent, RunConfig, Runner, RunState, Session, SQLiteSession
from agents.decorators import tool
from agents.exceptions import AgentsException
from agents.memory.resumable_session import ResumableSession, ResumableSessionError
from agents.result import RunResult, RunResultStreaming
from agents.run import AgentRunner
from agents.testing import ScriptedModel, UnexpectedModelCall
from tests.test_error_logging_redaction import _assert_secret_absent_from_agents_traceback
from tests.test_responses import get_function_tool_call, get_text_message


class MemoryStore:
    """Model the store's atomic comparison and controllable acknowledgment boundary."""

    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.block_checkpoint = False
        self.fail_checkpoint = False
        self.saving = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, session_id: str) -> str | None:
        return self.records.get(session_id)

    async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
        if expected is not None and json.loads(value)["status"] != "active":
            self.saving.set()
            if self.block_checkpoint:
                await self.release.wait()
            if self.fail_checkpoint:
                raise RuntimeError("synthetic checkpoint failure")
        if self.records.get(session_id) != expected:
            return False
        self.records[session_id] = value
        return True

    def checkpoint(self, session_id: str) -> dict[str, Any]:
        return json.loads(self.records[session_id])


CONFIG = RunConfig(tracing_disabled=True)


async def run_agent(
    agent: Agent[Any], input: Any, session: Session | ResumableSession | None, streamed: bool
) -> RunResult | RunResultStreaming:
    options: dict[str, Any] = {"run_config": CONFIG}
    if session is not None:
        options["session"] = session
    if not streamed:
        return await Runner.run(agent, input, **options)
    result = Runner.run_streamed(agent, input, **options)
    async for _ in result.stream_events():
        pass
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_completed_history_recovers_through_new_handle(streamed: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    first = ScriptedModel([[get_text_message("first answer")]])
    await run_agent(Agent(name="assistant", model=first), "first question", session, streamed)
    assert store.checkpoint(session.session_id)["status"] == "ready"
    assert not session.is_paused

    restored = await ResumableSession.resume(store, session.session_id)
    second = ScriptedModel([[get_text_message("second answer")]])
    result = await run_agent(Agent(name="assistant", model=second), "follow-up", restored, streamed)
    assert restored.session_id == session.session_id
    assert result.final_output == "second answer"
    sent = json.dumps(second.calls[0].input)
    assert "first question" in sent
    assert "first answer" in sent
    assert "follow-up" in sent
    history = store.checkpoint(session.session_id)["history"]
    assert len(history) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("approved", [False, True], ids=["reject", "approve"])
@pytest.mark.parametrize("explicit_session", [False, True], ids=["implicit", "explicit"])
async def test_fresh_handle_resolves_original_call_once(
    streamed: bool, approved: bool, explicit_session: bool
) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool(needs_approval=True)
    async def change(value: str) -> str:
        effects.append(value)
        return "original tool result"

    model = ScriptedModel(
        [[get_function_tool_call("change", '{"value":"original"}', call_id="original-call")]]
    )
    agent = Agent(name="assistant", model=model, tools=[change])
    paused = await run_agent(agent, "change it", session, streamed)
    assert len(paused.interruptions) == 1
    assert not effects
    checkpoint = store.checkpoint(session.session_id)
    assert checkpoint["status"] == "paused"
    assert session.is_paused
    assert "original-call" in checkpoint["run_state"]

    restored = await ResumableSession.resume(store, session.session_id)
    resumed_model = ScriptedModel([[get_text_message("resolved")]])
    rebuilt = Agent(name="assistant", model=resumed_model, tools=[change])
    state = await restored.load_state(rebuilt)
    (pending,) = state.get_interruptions()
    assert pending.raw_item.call_id == "original-call"
    if approved:
        state.approve(pending)
    else:
        state.reject(pending, rejection_message="explicit denial")
    result = await run_agent(rebuilt, state, restored if explicit_session else None, streamed)
    assert result.final_output == "resolved"
    assert not restored.is_paused
    assert effects == (["original"] if approved else [])
    history = store.checkpoint(session.session_id)["history"]
    calls = [item for item in history if item.get("type") == "function_call"]
    outputs = [item for item in history if item.get("type") == "function_call_output"]
    assert len(calls) == len(outputs) == 1
    assert calls[0]["call_id"] == outputs[0]["call_id"] == "original-call"
    assert ("original tool result" if approved else "explicit denial") in json.dumps(outputs)

    with pytest.raises(ResumableSessionError):
        await run_agent(rebuilt, state, restored, streamed)
    assert effects == (["original"] if approved else [])
    assert len(resumed_model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True], ids=["completed", "approval"])
async def test_stream_terminal_publication_waits_for_checkpoint(paused: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)

    @tool(needs_approval=True)
    def change() -> str:
        return "changed"

    output = (
        [get_function_tool_call("change", "{}", call_id="pending-call")]
        if paused
        else [get_text_message("confirmed answer")]
    )
    agent = Agent(name="assistant", model=ScriptedModel([output]), tools=[change])
    store.block_checkpoint = True
    result = Runner.run_streamed(agent, "start", session=session, run_config=CONFIG)
    events = []

    async def consume() -> None:
        async for event in result.stream_events():
            events.append(event)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(store.saving.wait(), timeout=5)
        assert store.checkpoint(session.session_id)["status"] == "active"
        assert result.final_output is None
        assert not result.interruptions
        assert not result.is_complete
        assert not any(
            e.type == "run_item_stream_event"
            and (e.name == "message_output_created" or e.item.type == "tool_approval_item")
            for e in events
        )
        with pytest.raises(AgentsException, match="not been acknowledged"):
            result.to_state()
    finally:
        store.release.set()
        await asyncio.wait_for(consumer, timeout=5)
    assert store.checkpoint(session.session_id)["status"] == ("paused" if paused else "ready")
    assert result.is_complete
    assert bool(result.interruptions) == paused
    assert any(e.type == "run_item_stream_event" for e in events)


@pytest.mark.asyncio
async def test_failed_final_save_retains_prior_checkpoint_and_unknown_attempt() -> None:
    streamed = True
    store = MemoryStore()
    session = await ResumableSession.create(store)
    await run_agent(
        Agent(name="assistant", model=ScriptedModel([[get_text_message("prior answer")]])),
        "prior question",
        session,
        streamed,
    )
    prior_history = store.checkpoint(session.session_id)["history"]
    store.fail_checkpoint = True
    model = ScriptedModel([[get_text_message("unconfirmed answer")]])
    agent = Agent(name="assistant", model=model)
    with pytest.raises(ResumableSessionError, match="could not confirm") as failure:
        await run_agent(agent, "next", session, streamed)
    assert failure.value.run_data is None
    active = store.checkpoint(session.session_id)
    assert active["status"] == "active"
    assert active["history"] == prior_history
    with pytest.raises(ResumableSessionError, match="unknown outcome"):
        await ResumableSession.resume(store, session.session_id)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["direct-run", "streamed"])
async def test_checkpoint_failure_and_subsequent_claim_release_run_payloads(streamed: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    store.fail_checkpoint = True
    secret = "synthetic-checkpoint-payload"
    model = ScriptedModel([[get_text_message(secret)]])
    agent = Agent(name="assistant", instructions=secret, model=model)

    for reason in ("unavailable", "active"):
        with pytest.raises(ResumableSessionError) as failure:
            if streamed:
                await run_agent(agent, secret, session, True)
            else:
                await AgentRunner().run(agent, secret, session=session, run_config=CONFIG)
        assert failure.value.reason == reason
        assert failure.value.run_data is None
        _assert_secret_absent_from_agents_traceback(failure.value, secret)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_stale_handle_fails_before_inference() -> None:
    streamed = False
    store = MemoryStore()
    session = await ResumableSession.create(store)
    stale = await ResumableSession.resume(store, session.session_id)
    await run_agent(
        Agent(name="assistant", model=ScriptedModel([[get_text_message("winner")]])),
        "first",
        session,
        streamed,
    )
    model = ScriptedModel([[get_text_message("must not be called")]])
    with pytest.raises(ResumableSessionError, match="changed"):
        await run_agent(Agent(name="assistant", model=model), "stale", stale, streamed)
    assert not model.calls
    assert store.checkpoint(session.session_id)["status"] == "ready"


@pytest.mark.asyncio
async def test_paused_session_rejects_new_input_before_inference() -> None:
    streamed = False
    store = MemoryStore()
    session = await ResumableSession.create(store)

    @tool(needs_approval=True)
    def change() -> str:
        return "changed"

    agent = Agent(
        name="assistant",
        model=ScriptedModel([[get_function_tool_call("change", "{}", call_id="saved-call")]]),
        tools=[change],
    )
    await run_agent(agent, "start", session, streamed)
    restored = await ResumableSession.resume(store, session.session_id)
    untouched = ScriptedModel([[get_text_message("must not be called")]])
    with pytest.raises(ResumableSessionError, match="saved approval state"):
        await run_agent(
            Agent(name="assistant", model=untouched, tools=[change]),
            "new prompt",
            restored,
            streamed,
        )
    assert not untouched.calls
    assert store.checkpoint(session.session_id)["status"] == "paused"


@pytest.mark.asyncio
async def test_effect_followed_by_failure_stays_unknown_without_automatic_replay() -> None:
    streamed = False
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool
    async def change() -> str:
        effects.append("changed")
        return "effect completed"

    model = ScriptedModel([[get_function_tool_call("change", "{}", call_id="effect-call")]])
    agent = Agent(name="assistant", model=model, tools=[change])
    with pytest.raises(UnexpectedModelCall):
        # The second model request has no scripted response, after the real tool effect.
        await run_agent(agent, "start", session, streamed)
    assert effects == ["changed"]
    checkpoint = store.checkpoint(session.session_id)
    assert checkpoint["status"] == "active"
    assert checkpoint["history"] == []
    with pytest.raises(ResumableSessionError, match="unknown outcome"):
        await ResumableSession.resume(store, session.session_id)
    assert effects == ["changed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["fresh-handle", "shared-handle", "lost-ack"])
async def test_competing_runs_do_not_dispatch_the_loser(competitor: str) -> None:
    streamed = competitor == "fresh-handle"
    from agents.testing import ModelCall, ModelStep

    class ContendedStore(MemoryStore):
        async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
            if (
                competitor == "lost-ack"
                and json.loads(value)["status"] == "active"
                and self.records.get(session_id) != expected
            ):
                raise TimeoutError("synthetic lost acknowledgment for losing claim")
            return await super().save(session_id, value, expected=expected)

    store = ContendedStore()
    winner = await ResumableSession.create(store)
    loser = (
        winner
        if competitor == "shared-handle"
        else await ResumableSession.resume(store, winner.session_id)
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(call: ModelCall) -> Any:
        entered.set()
        await release.wait()
        return [get_text_message("winner answer")]

    winner_model = ScriptedModel([ModelStep.respond(respond)])
    winner_task = asyncio.create_task(
        run_agent(Agent(name="assistant", model=winner_model), "winner", winner, streamed)
    )
    loser_model = ScriptedModel([[get_text_message("loser must not dispatch")]])
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(ResumableSessionError):
            await run_agent(Agent(name="assistant", model=loser_model), "loser", loser, streamed)
        assert not loser_model.calls
        assert store.checkpoint(winner.session_id)["status"] == "active"
    finally:
        release.set()
        winner_result = await asyncio.wait_for(winner_task, timeout=5)
    assert winner_result.final_output == "winner answer"
    assert store.checkpoint(winner.session_id)["status"] == "ready"
    assert len(winner_model.calls) == 1


@pytest.mark.asyncio
async def test_delayed_stream_claim_uses_history_after_shared_handle_advances(monkeypatch) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_begin = session._begin_run

    async def gated_begin(input: Any) -> None:
        if input == "later":
            entered.set()
            await release.wait()
        await original_begin(input)

    # Suspend only the claim boundary so another public run can advance the same handle.
    monkeypatch.setattr(session, "_begin_run", gated_begin)
    model = ScriptedModel([[get_text_message("later answer")]])
    result = Runner.run_streamed(
        Agent(name="assistant", model=model), "later", session=session, run_config=CONFIG
    )

    async def consume() -> None:
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await Runner.run(
            Agent(name="assistant", model=ScriptedModel([[get_text_message("earlier answer")]])),
            "earlier",
            session=session,
            run_config=CONFIG,
        )
    finally:
        release.set()
        await asyncio.wait_for(consumer, timeout=5)
    assert "earlier answer" in json.dumps(model.calls[0].input)
    assert len(store.checkpoint(session.session_id)["history"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["configuration", "hooks"])
async def test_invalid_run_setup_does_not_claim_a_ready_checkpoint(invalid: str) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    model = ScriptedModel([[get_text_message("must not dispatch")]])
    options: dict[str, Any] = (
        {"run_config": {"unknown_setting": True}}
        if invalid == "configuration"
        else {"hooks": object()}
    )
    with pytest.raises((TypeError, ValueError, AgentsException)):
        await Runner.run(Agent(name="assistant", model=model), "start", session=session, **options)
    assert not model.calls
    assert store.checkpoint(session.session_id)["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_error_handler_completion_confirms_a_ready_checkpoint(streamed: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    model = ScriptedModel()
    agent = Agent(name="assistant", model=model)
    options = {"max_turns": lambda _: "handled answer"}
    if streamed:
        result = Runner.run_streamed(
            agent, "start", session=session, max_turns=0, error_handlers=options, run_config=CONFIG
        )
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent, "start", session=session, max_turns=0, error_handlers=options, run_config=CONFIG
        )
    assert result.final_output == "handled answer"
    checkpoint = store.checkpoint(session.session_id)
    assert checkpoint["status"] == "ready"
    assert "handled answer" in json.dumps(checkpoint["history"])
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("copy_state", [False, True], ids=["live", "deepcopy"])
async def test_result_state_carries_the_confirmed_session_binding(
    streamed: bool, copy_state: bool
) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool(needs_approval=True)
    def change() -> str:
        effects.append("changed")
        return "confirmed output"

    model = ScriptedModel(
        [
            [get_function_tool_call("change", "{}", call_id="bound-call")],
            [get_text_message("resolved")],
        ]
    )
    agent = Agent(name="assistant", model=model, tools=[change])
    paused = await run_agent(agent, "start", session, streamed)
    state = paused.to_state()
    if copy_state:
        state = copy.deepcopy(state)
    (pending,) = state.get_interruptions()
    state.approve(pending)
    result = await run_agent(agent, state, None, streamed)
    assert result.final_output == "resolved"
    assert effects == ["changed"]
    assert store.checkpoint(session.session_id)["status"] == "ready"
    history = store.checkpoint(session.session_id)["history"]
    assert sum(item.get("call_id") == "bound-call" for item in history) == 2
    with pytest.raises(ResumableSessionError):
        await run_agent(agent, state, None, streamed)
    with pytest.raises(ResumableSessionError):
        await run_agent(agent, result.to_state(), None, streamed)
    assert effects == ["changed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("history_only", [False, True], ids=["other-durable", "history-only"])
async def test_bound_approval_rejects_a_conflicting_session_before_dispatch(
    streamed: bool, history_only: bool
) -> None:
    marker = "SYNTHETIC_BOUND_APPROVAL_PRIVATE_INPUT"
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool(needs_approval=True)
    def change() -> str:
        effects.append("changed")
        return "output"

    agent = Agent(
        name="assistant",
        instructions=marker,
        model=ScriptedModel([[get_function_tool_call("change", "{}", call_id="bound-call")]]),
        tools=[change],
    )
    await run_agent(agent, marker, session, streamed)
    saved = store.records[session.session_id]
    model = ScriptedModel([[get_text_message("must not dispatch")]])
    rebuilt = Agent(name="assistant", instructions=marker, model=model, tools=[change])
    state = await session.load_state(rebuilt)
    state.approve(state.get_interruptions()[0])
    conflicting = (
        SQLiteSession("history-only")
        if history_only
        else await ResumableSession.create(MemoryStore())
    )
    try:
        if streamed:
            result = Runner.run_streamed(rebuilt, state, session=conflicting, run_config=CONFIG)
            with pytest.raises(ResumableSessionError) as error:
                async for _ in result.stream_events():
                    pass
        else:
            with pytest.raises(ResumableSessionError) as error:
                await Runner.run(rebuilt, state, session=conflicting, run_config=CONFIG)
        _assert_secret_absent_from_agents_traceback(error.value, marker)
        assert error.value.reason == "invalid_state"
        assert not effects
        assert not model.calls
        assert store.records[session.session_id] == saved
        if isinstance(conflicting, SQLiteSession):
            assert await conflicting.get_items() == []
    finally:
        if isinstance(conflicting, SQLiteSession):
            conflicting.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("explicit_session", [False, True], ids=["implicit", "explicit"])
async def test_detached_approval_requires_loading_through_the_session(
    streamed: bool, explicit_session: bool
) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool(needs_approval=True)
    def change() -> str:
        effects.append("changed")
        return "output"

    agent = Agent(
        name="assistant",
        model=ScriptedModel([[get_function_tool_call("change", "{}", call_id="bound-call")]]),
        tools=[change],
    )
    paused = await run_agent(agent, "start", session, streamed)
    model = ScriptedModel([[get_text_message("resolved")]])
    rebuilt = Agent(name="assistant", model=model, tools=[change])
    detached = await RunState.from_string(rebuilt, paused.to_state().to_string())
    detached.approve(detached.get_interruptions()[0])
    with pytest.raises(ResumableSessionError, match="session.load_state"):
        await run_agent(rebuilt, detached, session if explicit_session else None, streamed)
    assert not effects
    assert not model.calls
    assert session.is_paused
    attached = await session.load_state(rebuilt)
    attached.approve(attached.get_interruptions()[0])
    await run_agent(rebuilt, attached, None, streamed)
    assert effects == ["changed"]
    assert store.checkpoint(session.session_id)["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_independently_loaded_approvals_claim_one_revision_once(streamed: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []

    @tool(needs_approval=True)
    def change() -> str:
        effects.append("changed")
        return "output"

    agent = Agent(
        name="assistant",
        model=ScriptedModel([[get_function_tool_call("change", "{}", call_id="bound-call")]]),
        tools=[change],
    )
    await run_agent(agent, "start", session, streamed)
    restored = [await ResumableSession.resume(store, session.session_id) for _ in range(2)]
    agents = [
        Agent(
            name="assistant", model=ScriptedModel([[get_text_message("resolved")]]), tools=[change]
        )
        for _ in range(2)
    ]
    states = [
        await owner.load_state(worker) for owner, worker in zip(restored, agents, strict=True)
    ]
    for state in states:
        state.approve(state.get_interruptions()[0])
    outcomes = await asyncio.gather(
        *(
            run_agent(worker, state, None, streamed)
            for worker, state in zip(agents, states, strict=True)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, ResumableSessionError) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, (RunResult, RunResultStreaming)) for outcome in outcomes) == 1
    assert effects == ["changed"]
    assert store.checkpoint(session.session_id)["status"] == "ready"
    history = store.checkpoint(session.session_id)["history"]
    assert sum(item.get("call_id") == "bound-call" for item in history) == 2
