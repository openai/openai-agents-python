from __future__ import annotations

import json

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    RunState,
    StopAtTools,
    function_tool,
    output_guardrail,
)
from agents.agent import Agent as AgentType
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


def make_agent() -> Agent:
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
        tool_use_behavior=StopAtTools(stop_at_tool_names=["finish"]),
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
