from __future__ import annotations

import json
from typing import Literal

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    StopAtTools,
    function_tool,
    output_guardrail,
)
from agents.agent import Agent as AgentType
from agents.items import TResponseInputItem
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession


@function_tool(name_override="write_thing", needs_approval=True)
def write_thing(query: str) -> str:
    return f"wrote:{query}"


@function_tool(name_override="look_up", needs_approval=False)
def look_up(query: str) -> str:
    return f"schema for {query}"


@output_guardrail
async def always_fine(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)


DEFERRING_BEHAVIOR = StopAtTools(stop_at_tool_names=["finish"])


def make_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = DEFERRING_BEHAVIOR,
) -> Agent:
    return Agent(
        name="deferred repro",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                # Two model turns before the interruption, like a real agent: an
                # ungated lookup first, THEN the gated write. The interruption must
                # land on turn > 1 so the resumed boundary has an accepted prefix.
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[look_up, write_thing],
        # The two conditions that open ``_should_defer_interrupted_session_items``:
        # output guardrails AND ``tool_use_behavior != "run_llm_again"``. The approved
        # tool is NOT in the stop list, so the resume resolves into
        # ``next_step_run_again`` rather than a terminal tool output.
        output_guardrails=[always_fine],
        tool_use_behavior=tool_use_behavior,
    )


@pytest.mark.asyncio
async def test_deferred_parked_call_is_persisted_when_the_resume_runs_again() -> None:
    """An approved tool's ``function_call`` must reach the Session, not only its output.

    With output guardrails and a non-default ``tool_use_behavior``, the interrupted
    turn's session items are deferred at interruption time
    (``_should_defer_interrupted_session_items``). When the approval resume resolves
    into ``next_step_run_again``, the resume-side write only carries the resolved
    turn's new items (the tool output), and no later write recovers the deferred
    ``function_call``. The Session ends up with a ``function_call_output`` whose call
    was never persisted, and the Responses API rejects every later run over that
    Session with "No tool call found for function call output".
    """
    session = SimpleListSession()
    agent = make_agent()

    first = Runner.run_streamed(agent, "do the thing", session=session)
    async for _ in first.stream_events():
        pass
    assert len(first.interruptions) == 1

    # Park in an external store and resume from it, as a multi-process app must:
    # the RunState round-trips through JSON between the two runs.
    serialized = json.dumps(first.to_state().to_json())
    state = await RunState.from_json(agent, json.loads(serialized))
    state.approve(state.get_interruptions()[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    async for _ in resumed.stream_events():
        pass
    assert resumed.final_output == "done"

    items = await session.get_items()
    call_ids = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    orphaned = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in call_ids
    ]
    assert orphaned == [], (
        "the approved tool's function_call never reached the Session; "
        f"orphaned outputs: {[item.get('call_id') for item in orphaned]}"
    )
    assert "call_PARKED" in call_ids


@pytest.mark.asyncio
async def test_park_time_deferral_survives_a_tool_use_behavior_change_on_resume() -> None:
    """The deferral decision is the checkpoint's, not the resuming configuration's.

    Parking defers the interrupted turn's write (guardrails + non-default
    ``tool_use_behavior``); the caller then resumes with ``"run_llm_again"``. Deriving
    the decision from today's gate would conclude nothing was deferred and drop the
    parked ``function_call`` again — it must come from checkpoint state instead
    (``_current_turn_persisted_item_count``).
    """
    session = SimpleListSession()

    first = Runner.run_streamed(make_agent(), "do the thing", session=session)
    async for _ in first.stream_events():
        pass
    assert len(first.interruptions) == 1

    resume_agent = make_agent(tool_use_behavior="run_llm_again")
    serialized = json.dumps(first.to_state().to_json())
    state = await RunState.from_json(resume_agent, json.loads(serialized))
    state.approve(state.get_interruptions()[0])

    resumed = Runner.run_streamed(resume_agent, state, session=session)
    async for _ in resumed.stream_events():
        pass

    items = await session.get_items()
    call_ids = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    orphaned = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in call_ids
    ]
    assert orphaned == []
    assert "call_PARKED" in call_ids


@pytest.mark.asyncio
async def test_non_deferred_park_is_not_double_written_on_resume() -> None:
    """The other direction of deriving from state: with ``"run_llm_again"`` throughout,
    the interruption-time write runs (no deferral) and bumps the persisted count, so the
    resume must not write the parked ``function_call`` a second time."""
    session = SimpleListSession()
    agent = make_agent(tool_use_behavior="run_llm_again")

    first = Runner.run_streamed(agent, "do the thing", session=session)
    async for _ in first.stream_events():
        pass
    assert len(first.interruptions) == 1

    serialized = json.dumps(first.to_state().to_json())
    state = await RunState.from_json(agent, json.loads(serialized))
    state.approve(state.get_interruptions()[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    async for _ in resumed.stream_events():
        pass

    items = await session.get_items()
    parked_calls = [
        item
        for item in items
        if item.get("type") == "function_call" and item.get("call_id") == "call_PARKED"
    ]
    parked_outputs = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") == "call_PARKED"
    ]
    assert len(parked_calls) == 1
    assert len(parked_outputs) == 1


@function_tool(name_override="write_other", needs_approval=True)
def write_other(query: str) -> str:
    return f"other:{query}"


def make_multi_approval_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = DEFERRING_BEHAVIOR,
) -> Agent:
    """One deferred model response carrying TWO approval-required calls."""
    return Agent(
        name="deferred repro (multi)",
        instructions="Call both tools.",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("write_other", {"query": "x"}, call_id="call_PARKED_2"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        output_guardrails=[always_fine],
        tool_use_behavior=tool_use_behavior,
    )


@pytest.mark.asyncio
async def test_partial_approval_reinterruption_persists_the_deferred_prefix() -> None:
    """A resume that interrupts AGAIN must not strand the deferred calls.

    Two approval-required calls in one deferred response; the caller approves only one
    and resumes with ``"run_llm_again"`` (gate closed). The resume resolves back into
    ``NextStepInterruption``, and that re-interruption write is the deferred prefix's
    last chance: it bumps the persisted count, so writing only the approved tool's
    output there would orphan BOTH parked calls for every later resume.
    """
    session = SimpleListSession()

    first = Runner.run_streamed(make_multi_approval_agent(), "go", session=session)
    async for _ in first.stream_events():
        pass
    assert len(first.interruptions) == 2

    resume_agent = make_multi_approval_agent(tool_use_behavior="run_llm_again")
    serialized = json.dumps(first.to_state().to_json())
    state = await RunState.from_json(resume_agent, json.loads(serialized))
    first_approval = next(
        interruption
        for interruption in state.get_interruptions()
        if "call_PARKED" == getattr(interruption.raw_item, "call_id", None)
    )
    state.approve(first_approval)

    second = Runner.run_streamed(resume_agent, state, session=session)
    async for _ in second.stream_events():
        pass
    assert len(second.interruptions) == 1

    serialized = json.dumps(second.to_state().to_json())
    state = await RunState.from_json(resume_agent, json.loads(serialized))
    for interruption in state.get_interruptions():
        state.approve(interruption)
    final = Runner.run_streamed(resume_agent, state, session=session)
    async for _ in final.stream_events():
        pass
    assert final.final_output == "done"

    items = await session.get_items()
    call_ids = [item.get("call_id") for item in items if item.get("type") == "function_call"]
    orphaned = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in call_ids
    ]
    assert orphaned == []
    # Each parked call exactly once: recovered by the re-interruption write, and not
    # written again by the later resumes (the persisted count now covers it).
    assert call_ids.count("call_PARKED") == 1
    assert call_ids.count("call_PARKED_2") == 1


def make_terminal_tool_agent(with_guardrails: bool = True) -> Agent:
    """The approved tool IS terminal, so the resume ends in a final output."""
    return Agent(
        name="deferred repro (terminal)",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=[always_fine] if with_guardrails else [],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["write_thing"]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_with_guardrails", [True, False])
@pytest.mark.parametrize("streamed", [True, False])
async def test_deferred_prefix_reaches_a_resume_that_ends_in_final_output(
    resume_with_guardrails: bool, streamed: bool
) -> None:
    """The final-output exit needs the prefix too, in both runners.

    ``_final_turn_items_for_persistence`` rebuilds the whole current response ONLY when
    the agent has output guardrails; without them it returns the turn's items verbatim.
    A resume may legitimately run without the guardrails the park had, and then the
    deferred ``function_call`` was dropped on this exit.
    """
    session = SimpleListSession()

    async def go(agent: Agent, run_input: object) -> object:
        if streamed:
            result = Runner.run_streamed(agent, run_input, session=session)  # type: ignore[arg-type]
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, run_input, session=session)  # type: ignore[arg-type]

    first = await go(make_terminal_tool_agent(), "do the thing")
    assert len(first.interruptions) == 1  # type: ignore[attr-defined]

    resume_agent = make_terminal_tool_agent(with_guardrails=resume_with_guardrails)
    serialized = json.dumps(first.to_state().to_json())  # type: ignore[attr-defined]
    state = await RunState.from_json(resume_agent, json.loads(serialized))
    state.approve(state.get_interruptions()[0])
    await go(resume_agent, state)

    items = await session.get_items()
    call_ids = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    orphaned = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in call_ids
    ]
    assert orphaned == []
    assert "call_PARKED" in call_ids


@pytest.mark.asyncio
async def test_a_detached_resume_does_not_make_the_next_one_rewrite_the_session() -> None:
    """``persisted_count`` can lie, so the prefix is confirmed against the Session.

    ``_validate_resumed_session_output_guardrail_safety`` resets the counter to zero for a
    DETACHED resume ("a detached Session cannot contribute its old persisted prefix").
    That reset outlives the run, so a later resume reconnecting the original Session sees
    zero and would rewrite items the Session already holds.
    """
    session = SimpleListSession()

    parked = Runner.run_streamed(
        make_multi_approval_agent(tool_use_behavior="run_llm_again"), "go", session=session
    )
    async for _ in parked.stream_events():
        pass
    assert len(parked.interruptions) == 2
    # No deferral at park time: the interrupted turn's items ARE persisted.
    persisted_at_park = [item.get("call_id") for item in await session.get_items()]
    assert "call_PARKED" in persisted_at_park

    deferring_agent = make_multi_approval_agent()
    state = await RunState.from_json(
        deferring_agent, json.loads(json.dumps(parked.to_state().to_json()))
    )
    state.approve(
        next(
            interruption
            for interruption in state.get_interruptions()
            if getattr(interruption.raw_item, "call_id", None) == "call_PARKED"
        )
    )
    detached = Runner.run_streamed(deferring_agent, state, session=None)
    async for _ in detached.stream_events():
        pass

    state = await RunState.from_json(
        deferring_agent, json.loads(json.dumps(detached.to_state().to_json()))
    )
    for interruption in state.get_interruptions():
        state.approve(interruption)
    reconnected = Runner.run_streamed(deferring_agent, state, session=session)
    async for _ in reconnected.stream_events():
        pass

    call_ids = [
        item.get("call_id")
        for item in await session.get_items()
        if item.get("type") == "function_call"
    ]
    assert call_ids.count("call_PARKED") == 1
    assert call_ids.count("call_PARKED_2") == 1


def make_emptying_handoff_agent() -> Agent:
    """One response carrying the gated call AND a handoff whose filter empties the turn.

    Resolving the approval then produces a turn with no session items at all: the shape
    where a deferred prefix has nothing to ride on.
    """
    from agents import HandoffInputData, handoff

    def empties(data: HandoffInputData) -> HandoffInputData:
        return HandoffInputData(
            input_history=data.input_history, pre_handoff_items=(), new_items=()
        )

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(output=[assistant_message("done")]),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
    )
    return Agent(
        name="deferred repro (emptied turn)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing],
        handoffs=[handoff(target, input_filter=empties)],
        output_guardrails=[always_fine],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["finish"]),
    )


@pytest.mark.asyncio
async def test_an_emptied_resolved_turn_corrupts_nothing_in_either_runner() -> None:
    """When the resolved turn has no session items, the deferred prefix must not be
    written on its own: a call with no output poisons the Session exactly as the orphaned
    output does. Both runners must also agree, item for item; a divergence here is how a
    dangling-call regression would first show up.
    """

    async def run_case(streamed: bool) -> list[TResponseInputItem]:
        session = SimpleListSession()
        agent = make_emptying_handoff_agent()
        first: RunResult | RunResultStreaming
        if streamed:
            first = Runner.run_streamed(agent, "go", session=session)
            async for _ in first.stream_events():
                pass
        else:
            first = await Runner.run(agent, "go", session=session)
        assert len(first.interruptions) == 1
        serialized = json.dumps(first.to_state().to_json())
        state = await RunState.from_json(agent, json.loads(serialized))
        state.approve(state.get_interruptions()[0])
        if streamed:
            resumed = Runner.run_streamed(agent, state, session=session)
            async for _ in resumed.stream_events():
                pass
        else:
            await Runner.run(agent, state, session=session)
        return await session.get_items()

    streamed_items = await run_case(streamed=True)
    non_streamed_items = await run_case(streamed=False)

    for items in (streamed_items, non_streamed_items):
        calls = {i.get("call_id") for i in items if i.get("type") == "function_call"}
        outputs = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
        assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"

    assert [(i.get("type") or i.get("role"), i.get("call_id")) for i in streamed_items] == [
        (i.get("type") or i.get("role"), i.get("call_id")) for i in non_streamed_items
    ]
