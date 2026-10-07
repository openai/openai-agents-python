from __future__ import annotations

import json
from typing import Any

import pytest

from agents import (
    Agent,
    RunContextWrapper,
    Runner,
)
from agents.lifecycle import RunHooks
from agents.run import RunConfig
from agents.testing import ModelStep, ScriptedModel, function_call
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _DEFERRING_BEHAVIOR,
    _EXPECTED_PAIR,
    _make_deferring_agent,
    _parked_and_approved,
    _parked_pair,
    _run,
    _serialized_round_trip,
    always_fine,
    look_up,
    write_thing,
)


class _FinalOutputHookFailure(RunHooks[Any]):
    """Fail the run at the final-output hook, after the terminal step is decided."""

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        raise RuntimeError("final output hook failed")


@pytest.mark.asyncio
async def test_a_failed_max_turns_finalization_keeps_the_held_record() -> None:
    # The batch is disposed of when the run actually ends, not when the terminal step
    # is chosen. Validation, the final-output hooks and the output guardrails all run
    # after that choice and all can raise, and a run that raises may still be retried
    # or reattached with the executed tool's call and output reachable only here.
    from agents.run_internal.run_loop import finalize_max_turns_handler_output

    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=False)
    assert state._pending_session_write is not None

    async def _no_save(items: list[Any]) -> None:
        return None

    with pytest.raises(RuntimeError):
        await finalize_max_turns_handler_output(
            agent=agent,
            hooks=_FinalOutputHookFailure(),
            run_config=RunConfig(tracing_disabled=True),
            output="stopped at max turns",
            context_wrapper=RunContextWrapper(context=None),
            output_guardrail_results=[],
            save_items_after_guardrails=_no_save,
            include_in_history=False,
            run_state=state,
        )

    assert state._pending_session_write is not None


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
async def test_a_rejected_max_turns_handler_output_keeps_the_held_record() -> None:
    # The discard belongs to a handler that actually ends the run. Validation rejects a
    # wrongly typed handler output by raising, and the streamed runner discards only
    # after its finalization completes, so discarding ahead of the raise would leave
    # the caller's live RunState without a batch its streamed twin still holds.
    from agents.exceptions import UserError
    from agents.run_internal.run_loop import finalize_max_turns_handler_output

    session = SimpleListSession()
    agent = _make_deferring_agent()
    agent.output_type = int
    state = await _parked_and_approved(agent, session, streamed=False)
    assert state._pending_session_write is not None

    async def _no_save(items: list[Any]) -> None:
        return None

    with pytest.raises(UserError):
        await finalize_max_turns_handler_output(
            agent=agent,
            hooks=RunHooks(),
            run_config=RunConfig(tracing_disabled=True),
            output="not an int",
            context_wrapper=RunContextWrapper(context=None),
            output_guardrail_results=[],
            save_items_after_guardrails=_no_save,
            include_in_history=False,
            run_state=state,
        )

    assert state._pending_session_write is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_max_turns_handler_completion_clears_the_held_record(streamed: bool) -> None:
    # A max-turn handler ends the run, so a held batch still standing has no later
    # gate-legal exit to settle it: both runners must report the same terminal state,
    # with no pending write left to invalidate the finished run's checkpoint.
    from agents.run_internal.run_loop import finalize_max_turns_handler_output

    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    assert state._pending_session_write is not None

    async def _no_save(items: list[Any]) -> None:
        return None

    await finalize_max_turns_handler_output(
        agent=agent,
        hooks=RunHooks(),
        run_config=RunConfig(tracing_disabled=True),
        output="stopped at max turns",
        context_wrapper=RunContextWrapper(context=None),
        output_guardrail_results=[],
        save_items_after_guardrails=_no_save,
        include_in_history=False,
        run_state=state,
    )

    assert state._pending_session_write is None


def _make_never_finishing_agent() -> Agent:
    """Parks on turn two, then keeps calling tools so max turns is what ends the run."""
    steps = [
        ModelStep(output=[function_call("look_up", {"query": "a"}, call_id="call_LOOKUP")]),
        ModelStep(output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]),
    ]
    steps += [
        ModelStep(output=[function_call("look_up", {"query": f"q{i}"}, call_id=f"call_L{i}")])
        for i in range(8)
    ]
    return Agent(
        name="deferred repro (never finishing)",
        instructions="x",
        model=ScriptedModel(steps),
        tools=[look_up, write_thing],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


@pytest.mark.asyncio
async def test_a_streamed_max_turns_completion_clears_the_held_record() -> None:
    # The streaming runner reaches its max-turn handler through its own terminal path,
    # not the shared helper, so it needs its own coverage: a detached resume that runs
    # out of turns must not report terminal handler output while carrying a resumable
    # pending write the non-streaming runner had already dropped.
    session = SimpleListSession()
    agent = _make_never_finishing_agent()
    first = await _run(agent, "go", session, streamed=True)
    assert len(first.interruptions) == 1
    state = await _serialized_round_trip(first, agent)
    state.approve(state.get_interruptions()[0])
    assert state._pending_session_write is not None

    resumed = Runner.run_streamed(
        agent,
        state,
        session=None,
        max_turns=3,
        error_handlers={"max_turns": lambda data: "stopped at max turns"},
    )
    async for _ in resumed.stream_events():
        pass

    assert resumed.final_output == "stopped at max turns"
    assert "pending_session_write" not in resumed.to_state().to_json()
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("include_in_history", [False, True])
async def test_max_turns_fallback_preserves_approved_tool_history(
    streamed: bool, include_in_history: bool
) -> None:
    from agents.run_error_handlers import (
        RunErrorHandlerInput,
        RunErrorHandlerResult,
        RunErrorHandlers,
    )

    session = SimpleListSession()
    agent = _make_never_finishing_agent()
    if streamed:
        first = Runner.run_streamed(agent, "go", session=session, max_turns=3)
        async for _ in first.stream_events():
            pass
    else:
        first = await Runner.run(agent, "go", session=session, max_turns=3)
    state = await _serialized_round_trip(first, agent)
    state.approve(state.get_interruptions()[0])

    async def fallback(data: RunErrorHandlerInput[Any]) -> RunErrorHandlerResult:
        # Completed tool history is already durable before the handler runs.
        assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR
        return RunErrorHandlerResult(
            final_output="stopped at max turns", include_in_history=include_in_history
        )

    handlers: RunErrorHandlers[Any] = {"max_turns": fallback}
    if streamed:
        result = Runner.run_streamed(agent, state, session=session, error_handlers=handlers)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, state, session=session, error_handlers=handlers)
    assert result.final_output == "stopped at max turns"
    history = await session.get_items()
    assert _parked_pair(history) == _EXPECTED_PAIR
    assert ("stopped at max turns" in json.dumps(history)) == include_in_history
    assert "pending_session_write" not in result.to_state().to_json()
