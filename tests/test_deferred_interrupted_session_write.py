from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal, cast

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
from agents.exceptions import OutputGuardrailTripwireTriggered
from agents.items import TResponseInputItem
from agents.lifecycle import RunHooks
from agents.memory.openai_conversations_session import OpenAIConversationsSession
from agents.run import RunConfig
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession


@function_tool(name_override="write_thing", needs_approval=True)
def write_thing(query: str) -> str:
    return f"wrote:{query}"


@function_tool(name_override="write_other", needs_approval=True)
def write_other(query: str) -> str:
    return f"other:{query}"


@function_tool(name_override="look_up", needs_approval=False)
def look_up(query: str) -> str:
    return f"schema for {query}"


@output_guardrail
async def always_fine(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)


@output_guardrail
async def always_trips(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)


@output_guardrail
async def always_crashes(
    ctx: RunContextWrapper[object], agent: AgentType[object], output: object
) -> GuardrailFunctionOutput:
    raise RuntimeError("guardrail crashed")


# The two conditions that open ``_should_defer_interrupted_session_items``: output
# guardrails and a non-default ``tool_use_behavior``. The approved tool is not in the
# stop list, so the resume resolves into a run-again step rather than a terminal tool
# output.
_DEFERRING_BEHAVIOR = StopAtTools(stop_at_tool_names=["finish"])


def _make_deferring_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = _DEFERRING_BEHAVIOR,
) -> Agent:
    """A gated write on the second model turn, so the resumed boundary has a prefix."""
    return Agent(
        name="deferred repro",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=[always_fine],
        tool_use_behavior=tool_use_behavior,
    )


def _make_multi_approval_agent(
    tool_use_behavior: StopAtTools | Literal["run_llm_again"] = _DEFERRING_BEHAVIOR,
) -> Agent:
    """One deferred model response carrying two approval-required calls."""
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


_PREAMBLE_TEXT = "About to write the thing."


def _make_terminal_tool_agent(
    *,
    with_guardrails: bool = True,
    tripping: bool = False,
    crashing: bool = False,
    with_preamble: bool = False,
) -> Agent:
    """The approved tool is terminal, so the resume ends in a final output."""
    guardrails = [always_fine]
    if tripping:
        guardrails = [always_trips]
    if crashing:
        guardrails = [always_crashes]
    parked_response = [function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
    if with_preamble:
        parked_response = [assistant_message(_PREAMBLE_TEXT), *parked_response]
    return Agent(
        name="deferred repro (terminal)",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(output=parked_response),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=guardrails if with_guardrails else [],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["write_thing"]),
    )


def _make_emptying_handoff_agent() -> Agent:
    """The gated call rides one response with a handoff whose filter empties the turn."""
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
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


def _make_partial_filter_handoff_agent() -> Agent:
    """Two gated calls plus a handoff whose filter drops exactly one resolved output.

    An ``input_filter`` is an arbitrary caller callable, so dropping a subset of the
    resolved outputs is a legitimate shape; the held batch must not settle a call whose
    output the filter took away.
    """
    from agents import HandoffInputData, handoff

    def drops_one_output(data: HandoffInputData) -> HandoffInputData:
        def keep(items: tuple) -> tuple:
            kept = []
            for item in items:
                raw = getattr(item, "raw_item", None)
                call_id = (
                    raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                )
                if call_id == "call_PARKED_2" and item.type == "tool_call_output_item":
                    continue
                kept.append(item)
            return tuple(kept)

        return HandoffInputData(
            input_history=data.input_history,
            pre_handoff_items=keep(data.pre_handoff_items),
            new_items=keep(data.new_items),
        )

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel([ModelStep(output=[assistant_message("done")])]),
    )
    return Agent(
        name="deferred repro (partial filter)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("write_other", {"query": "x"}, call_id="call_PARKED_2"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        handoffs=[handoff(target, input_filter=drops_one_output)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


class _ContextRequiringSession(SimpleListSession):
    """Track whether internal reads and writes carry the run's context wrapper."""

    def __init__(self) -> None:
        super().__init__()
        self.wrapperless_operations = 0

    async def get_items(
        self, limit: int | None = None, *, wrapper: RunContextWrapper[Any] | None = None
    ) -> list[TResponseInputItem]:
        if limit is not None and wrapper is None:
            self.wrapperless_operations += 1
        return await super().get_items(limit)

    async def add_items(
        self, items: list[TResponseInputItem], *, wrapper: RunContextWrapper[Any] | None = None
    ) -> None:
        if wrapper is None:
            self.wrapperless_operations += 1
        await super().add_items(items)

    async def pop_item(
        self, *, wrapper: RunContextWrapper[Any] | None = None
    ) -> TResponseInputItem | None:
        return await super().pop_item()

    async def clear_session(self, *, wrapper: RunContextWrapper[Any] | None = None) -> None:
        await super().clear_session()


class _LegacyGetItemsSession(SimpleListSession):
    """A pre-limit Session whose ``get_items`` takes no arguments at all."""

    async def get_items(self) -> list[TResponseInputItem]:  # type: ignore[override]
        return await super().get_items()


class _AppendRecordingSession(SimpleListSession):
    """Record each ``add_items`` batch to observe write ordering and granularity."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[TResponseInputItem]] = []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.batches.append(list(items))
        await super().add_items(items)


class _FailingResumeSession(SimpleListSession):
    """Control append acknowledgement at the public Session boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None
        self.error = RuntimeError("session append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error
        await super().add_items(items)
        if failure == "after":
            raise self.error


async def _run(
    agent: Agent, run_input: Any, session: Any, *, streamed: bool
) -> RunResult | RunResultStreaming:
    if streamed:
        result = Runner.run_streamed(agent, run_input, session=session)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, run_input, session=session)


async def _serialized_round_trip(result: RunResult | RunResultStreaming, agent: Agent) -> RunState:
    return await RunState.from_json(agent, json.loads(json.dumps(result.to_state().to_json())))


def _call_ids(items: list[TResponseInputItem]) -> list[Any]:
    return [item.get("call_id") for item in items if item.get("type") == "function_call"]


def _orphaned_outputs(items: list[TResponseInputItem]) -> list[Any]:
    calls = set(_call_ids(items))
    return [
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") not in calls
    ]


def _parked_pair(items: list[TResponseInputItem]) -> list[str]:
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, dict) and item.get("call_id") == "call_PARKED"
    ]


async def _parked_and_approved(
    agent: Agent, session: Any, *, streamed: bool, resume_agent: Agent | None = None
) -> RunState:
    first = await _run(agent, "do the thing", session, streamed=streamed)
    assert len(first.interruptions) == 1
    state = await _serialized_round_trip(first, resume_agent or agent)
    state.approve(state.get_interruptions()[0])
    return state


_EXPECTED_PAIR = ["function_call", "function_call_output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_deferred_parked_call_is_persisted_when_the_resume_runs_again(
    streamed: bool,
) -> None:
    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    resumed = await _run(agent, state, session, streamed=streamed)
    assert resumed.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR
    assert "pending_session_write" not in resumed.to_state().to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_park_time_deferral_survives_a_tool_use_behavior_change_on_resume(
    streamed: bool,
) -> None:
    # The deferral decision is the checkpoint's, not the resuming configuration's: the
    # caller resumes with the default behavior, and deriving the decision from the live
    # gate would drop the parked call again.
    session = SimpleListSession()
    resume_agent = _make_deferring_agent(tool_use_behavior="run_llm_again")
    state = await _parked_and_approved(
        _make_deferring_agent(), session, streamed=streamed, resume_agent=resume_agent
    )

    await _run(resume_agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_non_deferred_park_is_not_double_written_on_resume(streamed: bool) -> None:
    # The other direction: with the default behavior throughout, the interruption-time
    # write runs, so the resume must not write the parked call a second time.
    session = SimpleListSession()
    agent = _make_deferring_agent(tool_use_behavior="run_llm_again")
    state = await _parked_and_approved(agent, session, streamed=streamed)

    await _run(agent, state, session, streamed=streamed)

    assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_partial_approval_reinterruption_keeps_one_canonical_batch(
    streamed: bool,
) -> None:
    # Two approval-required calls in one deferred response; approving only one resolves
    # into a second interruption. The held batch must absorb the resolved output and
    # keep riding as one record, then land exactly once when the run finally continues.
    session = SimpleListSession()
    agent = _make_multi_approval_agent()

    first = await _run(agent, "go", session, streamed=streamed)
    assert len(first.interruptions) == 2
    state = await _serialized_round_trip(first, agent)
    state.approve(
        next(
            interruption
            for interruption in state.get_interruptions()
            if getattr(interruption.raw_item, "call_id", None) == "call_PARKED"
        )
    )

    second = await _run(agent, state, session, streamed=streamed)
    assert len(second.interruptions) == 1
    second_checkpoint = second.to_state().to_json()
    pending = second_checkpoint.get("pending_session_write")
    assert pending is not None and pending.get("held") is True
    assert {item.get("call_id") for item in pending["items"]} == {
        "call_PARKED",
        "call_PARKED_2",
    }

    state = await RunState.from_json(agent, json.loads(json.dumps(second_checkpoint)))
    for interruption in state.get_interruptions():
        state.approve(interruption)
    final = await _run(agent, state, session, streamed=streamed)
    assert final.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _call_ids(items).count("call_PARKED") == 1
    assert _call_ids(items).count("call_PARKED_2") == 1
    # Every call must also keep its output: losing the first approval's output while
    # the batch rides the second park is the symmetric corruption.
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert set(_call_ids(items)) == outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_with_guardrails", [True, False])
@pytest.mark.parametrize("streamed", [False, True])
async def test_deferred_prefix_reaches_a_resume_that_ends_in_final_output(
    resume_with_guardrails: bool, streamed: bool
) -> None:
    # A resume may legitimately run without the guardrails the park had; the
    # final-output exit must land the held batch either way.
    session = SimpleListSession()
    resume_agent = _make_terminal_tool_agent(with_guardrails=resume_with_guardrails)
    state = await _parked_and_approved(
        _make_terminal_tool_agent(), session, streamed=streamed, resume_agent=resume_agent
    )

    await _run(resume_agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
async def test_a_detached_resume_does_not_make_the_next_one_rewrite_the_session() -> None:
    # A non-deferred park persists the interrupted turn's items; the resumed-safety
    # validation then zeroes the counter for a detached resume. A later resume that
    # reconnects the original Session must not rewrite items it already holds.
    session = SimpleListSession()

    parked = await _run(
        _make_multi_approval_agent(tool_use_behavior="run_llm_again"),
        "go",
        session,
        streamed=True,
    )
    assert len(parked.interruptions) == 2
    assert "call_PARKED" in _call_ids(await session.get_items())

    deferring_agent = _make_multi_approval_agent()
    state = await _serialized_round_trip(parked, deferring_agent)
    state.approve(
        next(
            interruption
            for interruption in state.get_interruptions()
            if getattr(interruption.raw_item, "call_id", None) == "call_PARKED"
        )
    )
    detached = await _run(deferring_agent, state, None, streamed=True)

    state = await _serialized_round_trip(detached, deferring_agent)
    for interruption in state.get_interruptions():
        state.approve(interruption)
    await _run(deferring_agent, state, session, streamed=True)

    call_ids = _call_ids(await session.get_items())
    assert call_ids.count("call_PARKED") == 1
    assert call_ids.count("call_PARKED_2") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_an_emptied_resolved_turn_settles_the_paired_part_of_the_held_batch(
    streamed: bool,
) -> None:
    # A handoff input_filter empties the resolved turn, but the approved tool already
    # ran and its output was folded into the held batch: pairing is the predicate, so
    # the executed pair settles and only the unpaired call drops. Discarding the whole
    # batch would lose the Session's only record that the tool ran, and the next run
    # would re-issue its side effect.
    session = SimpleListSession()
    agent = _make_emptying_handoff_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    resumed = await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
    assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"
    assert "call_PARKED" in calls, "the executed pair must survive the emptied turn"
    assert "call_HANDOFF" not in calls, "the unpaired call must not be written"
    assert "pending_session_write" not in resumed.to_state().to_json()
    # The discard must reach the live state too: a stale held record would invalidate
    # any checkpoint later taken from this completed run.
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_filter_that_drops_one_output_takes_its_held_call_with_it(
    streamed: bool,
) -> None:
    # The resolved turn is non-empty (one output survived the filter), so batch
    # emptiness is the wrong safety predicate: settling the whole held batch would land
    # the filtered call dangling, and discarding the whole batch would orphan the
    # output the filter kept. Pairing is the contract, per call.
    session = SimpleListSession()
    agent = _make_partial_filter_handoff_agent()
    first = await _run(agent, "go", session, streamed=streamed)
    state = await _serialized_round_trip(first, agent)
    for interruption in state.get_interruptions():
        state.approve(interruption)
    resumed = await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
    assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"
    assert "call_PARKED" in calls
    assert "pending_session_write" not in resumed.to_state().to_json()
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_held_resume_with_a_different_session_is_refused(streamed: bool) -> None:
    # The held entry skip must not bypass the same-session contract: resuming the
    # approval checkpoint against another Session would execute the tool and settle the
    # withheld batch into the wrong conversation.
    from agents.exceptions import UserError

    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    other_session = SimpleListSession("other")
    with pytest.raises(UserError, match="pending Session write"):
        await _run(agent, state, other_session, streamed=streamed)

    assert await other_session.get_items() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_settle_reaches_a_context_aware_session_through_the_wrapper(
    streamed: bool,
) -> None:
    session = _ContextRequiringSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    await _run(agent, state, session, streamed=streamed)

    assert session.wrapperless_operations == 0
    assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_session_without_optional_kwargs_survives_a_deferred_resume(
    streamed: bool,
) -> None:
    session = _LegacyGetItemsSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    resumed = await _run(agent, state, session, streamed=streamed)
    assert resumed.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
async def test_after_turn_cancel_keeps_the_held_batch_for_the_next_attach() -> None:
    # The detached carry: a detached resume executes the approved tool, an after-turn
    # cancel flips the checkpoint to a run-again step, and the batch must bring the
    # executed output to the reattaching resume. Cancellation only exists on the
    # streaming runner, so this scenario has no non-streamed axis.
    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=True)

    detached = Runner.run_streamed(agent, state, session=None)
    detached.cancel(mode="after_turn")
    async for _ in detached.stream_events():
        pass

    checkpoint = detached.to_state().to_json()
    pending = checkpoint.get("pending_session_write")
    assert pending is not None and pending.get("held") is True
    assert {item.get("call_id") for item in pending["items"]} >= {"call_PARKED"}

    state = await RunState.from_json(agent, json.loads(json.dumps(checkpoint)))
    reattached = Runner.run_streamed(agent, state, session=session)
    async for _ in reattached.stream_events():
        pass

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_reject_persists_the_parked_call_with_its_rejection_output(
    streamed: bool,
) -> None:
    session = SimpleListSession()
    agent = _make_deferring_agent()
    first = await _run(agent, "do the thing", session, streamed=streamed)
    assert len(first.interruptions) == 1
    state = await _serialized_round_trip(first, agent)
    state.reject(state.get_interruptions()[0])

    resumed = await _run(agent, state, session, streamed=streamed)
    assert resumed.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
async def test_the_held_batch_rides_a_non_streamed_result_into_its_checkpoint() -> None:
    # The non-streamed runner has no live RunState on a fresh park, so the declaration
    # must ride the result into ``to_state``; dropping it there is the one silent way
    # to lose the batch.
    session = SimpleListSession()
    agent = _make_deferring_agent()

    first = await Runner.run(agent, "do the thing", session=session)
    assert len(first.interruptions) == 1

    checkpoint = first.to_state().to_json()
    pending = checkpoint.get("pending_session_write")
    assert pending is not None and pending.get("held") is True
    assert "call_PARKED" in {item.get("call_id") for item in pending["items"]}
    assert pending.get("before") is None
    # The batch carries only the withheld response: the accepted input persists
    # eagerly even at a deferred park, so a tripwire discard can never take the
    # Session's only copy of the input with it.
    assert not any(item.get("role") == "user" for item in pending["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_the_settled_batch_and_the_resolved_turn_land_as_one_ordered_write(
    streamed: bool,
) -> None:
    # Settling separately from the resolved turn's save would either trip the
    # single-slot rule or advance the persisted count and slice the resolved items out
    # of their own save, so the pair must land in one append, call before output.
    session = _AppendRecordingSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    batches_before_resume = len(session.batches)

    await _run(agent, state, session, streamed=streamed)

    resume_batches = session.batches[batches_before_resume:]
    settling_batches = [
        batch for batch in resume_batches if "call_PARKED" in {i.get("call_id") for i in batch}
    ]
    assert len(settling_batches) == 1
    assert _parked_pair(settling_batches[0]) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_tripwire_after_approval_keeps_the_sanitized_pair(streamed: bool) -> None:
    session = SimpleListSession()
    resume_agent = _make_terminal_tool_agent(tripping=True, with_preamble=True)
    state = await _parked_and_approved(
        _make_terminal_tool_agent(with_preamble=True),
        session,
        streamed=streamed,
        resume_agent=resume_agent,
    )

    if streamed:
        resumed = Runner.run_streamed(resume_agent, state, session=session)
        with pytest.raises(OutputGuardrailTripwireTriggered):
            async for _ in resumed.stream_events():
                pass
        # The declaration is discarded when the blocked outcome is decided; a record
        # that outlives the tripwire would invalidate the run's checkpoint.
        assert "pending_session_write" not in resumed.to_state().to_json()
    else:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await Runner.run(resume_agent, state, session=session)

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR
    # The redaction drops the blocked response's preamble; feeding the raw held batch
    # into the blocked save would resurrect it.
    assert not any(_PREAMBLE_TEXT in json.dumps(item) for item in items)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_guardrail_crash_still_persists_the_parked_call(streamed: bool) -> None:
    session = SimpleListSession()
    resume_agent = _make_terminal_tool_agent(crashing=True)
    state = await _parked_and_approved(
        _make_terminal_tool_agent(), session, streamed=streamed, resume_agent=resume_agent
    )

    if streamed:
        resumed = Runner.run_streamed(resume_agent, state, session=session)
        with pytest.raises(RuntimeError, match="guardrail crashed"):
            async for _ in resumed.stream_events():
                pass
        assert "pending_session_write" not in resumed.to_state().to_json()
    else:
        with pytest.raises(RuntimeError, match="guardrail crashed"):
            await Runner.run(resume_agent, state, session=session)
    # The crash-path save claims the batch, so no stale record survives on the state.
    assert state._pending_session_write is None

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR


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


def _make_two_park_agent() -> Agent:
    """Two approval-required calls on consecutive turns, so a resume can park again."""
    return Agent(
        name="deferred repro (two parks)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("write_thing", {"query": "a"}, call_id="call_A")]),
                ModelStep(output=[function_call("write_other", {"query": "b"}, call_id="call_B")]),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing, write_other],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_new_park_during_a_detached_resume_joins_the_held_batch(
    streamed: bool,
) -> None:
    # A detached resume resolves the first approval and parks a second call on the next
    # turn. That fresh park cannot write anything, but the standing declaration carries
    # the session identity, so the new call must fold into the held batch or the
    # reattach settles its output orphaned.
    session = SimpleListSession()
    agent = _make_two_park_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    detached = await _run(agent, state, None, streamed=streamed)
    assert len(detached.interruptions) == 1
    state = await _serialized_round_trip(detached, agent)
    state.approve(state.get_interruptions()[0])

    reattached = await _run(agent, state, session, streamed=streamed)
    assert reattached.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls == outputs
    assert {"call_A", "call_B"} <= calls


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_gate_off_reinterruption_keeps_the_still_pending_call(streamed: bool) -> None:
    # Approving one of two held calls and resuming with the default behavior turns the
    # gate off, so the re-interruption exit settles the batch mid-run. The unapproved
    # call's output does not exist yet because it is still pending, not because a
    # filter removed it; dropping it there orphans its output on the final resume.
    session = SimpleListSession()
    resume_agent = _make_multi_approval_agent(tool_use_behavior="run_llm_again")

    first = await _run(_make_multi_approval_agent(), "go", session, streamed=streamed)
    assert len(first.interruptions) == 2
    state = await _serialized_round_trip(first, resume_agent)
    state.approve(
        next(
            interruption
            for interruption in state.get_interruptions()
            if getattr(interruption.raw_item, "call_id", None) == "call_PARKED"
        )
    )

    second = await _run(resume_agent, state, session, streamed=streamed)
    assert len(second.interruptions) == 1
    state = await _serialized_round_trip(second, resume_agent)
    for interruption in state.get_interruptions():
        state.approve(interruption)
    final = await _run(resume_agent, state, session, streamed=streamed)
    assert final.final_output == "done"

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls == outputs
    assert {"call_PARKED", "call_PARKED_2"} <= calls


@pytest.mark.asyncio
async def test_entry_settle_drops_a_held_call_the_filter_unpaired() -> None:
    # A detached resume of the partial-filter handoff folds the post-filter items into
    # the batch, the handoff normalizes the checkpoint to run-again, and an after-turn
    # cancellation stops the run there. The reattach settles at entry, where the same
    # pairing contract applies: the filtered call must not land dangling. Cancellation
    # only exists on the streaming runner, and this checkpoint shape resumes from the
    # live state.
    session = SimpleListSession()
    agent = _make_partial_filter_handoff_agent()
    first = await _run(agent, "go", session, streamed=True)
    state = await _serialized_round_trip(first, agent)
    for interruption in state.get_interruptions():
        state.approve(interruption)

    detached = Runner.run_streamed(agent, state, session=None)
    detached.cancel(mode="after_turn")
    async for _ in detached.stream_events():
        pass

    reattached = Runner.run_streamed(agent, detached.to_state(), session=session)
    async for _ in reattached.stream_events():
        pass

    items = await session.get_items()
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
    assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_detached_completion_clears_the_held_record(streamed: bool) -> None:
    # A detached resume that runs to completion has no Session to settle against and
    # the fresh final exit ends the run; a held record left standing would invalidate
    # the completed run's checkpoint and diverge between the runners.
    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)

    detached = await _run(agent, state, None, streamed=streamed)
    assert detached.final_output == "done"
    assert "pending_session_write" not in detached.to_state().to_json()
    assert state._pending_session_write is None


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


class _RecordingConversationsSession(OpenAIConversationsSession):
    """Stand-in carrying the Conversations class identity the settle checks.

    The real backend talks to the Conversations API; the settle only asks whether the
    session is one of these to decide that the batch needs the Conversations
    sanitization, so this records what would be sent instead of sending it.
    """

    def __init__(self) -> None:
        self.session_id = "conv-1"
        self.added: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        return []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.added.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return None

    async def clear_session(self) -> None:
        return None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_terminal_resume_with_a_preamble_lands_it_once(streamed: bool) -> None:
    # With output guardrails the final sweep rebuilds the whole current response, held
    # batch included; the deduplication cannot key the assistant preamble, so feeding
    # the batch again used to land the preamble twice.
    session = SimpleListSession()
    agent = _make_terminal_tool_agent(with_preamble=True)
    state = await _parked_and_approved(agent, session, streamed=streamed)
    await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert _orphaned_outputs(items) == []
    assert _parked_pair(items) == _EXPECTED_PAIR
    preambles = [item for item in items if _PREAMBLE_TEXT in json.dumps(item)]
    assert len(preambles) == 1


def test_the_pairing_guard_speaks_every_approval_identity() -> None:
    # A hosted MCP approval request identifies itself with ``id`` and its response
    # points back with ``approval_request_id``; custom calls pair by ``call_id``. A
    # request kind the guard cannot key would settle alone and poison the Session the
    # same way an unpaired function call does.
    from agents.run_internal.session_persistence import _held_items_safe_to_settle

    unpaired_mcp: TResponseInputItem = {
        "type": "mcp_approval_request",
        "id": "mcpr_1",
        "name": "do_it",
        "server_label": "srv",
        "arguments": "{}",
    }
    paired_mcp: TResponseInputItem = {
        "type": "mcp_approval_request",
        "id": "mcpr_2",
        "name": "do_it",
        "server_label": "srv",
        "arguments": "{}",
    }
    mcp_response: TResponseInputItem = {
        "type": "mcp_approval_response",
        "approval_request_id": "mcpr_2",
        "approve": True,
    }
    unpaired_custom: TResponseInputItem = {
        "type": "custom_tool_call",
        "call_id": "cust_1",
        "name": "custom",
        "input": "",
    }
    preamble: TResponseInputItem = {"role": "assistant", "content": "hi", "type": "message"}

    kept = _held_items_safe_to_settle(
        [unpaired_mcp, paired_mcp, mcp_response, unpaired_custom, preamble], [], None
    )
    assert kept == [paired_mcp, mcp_response, preamble]

    still_pending = _held_items_safe_to_settle(
        [unpaired_mcp], [], None, pending_call_ids={"mcpr_1"}
    )
    assert still_pending == [unpaired_mcp]


def test_the_pairing_guard_prunes_with_the_canonical_rule() -> None:
    # The prune delegates to drop_orphan_function_calls, so every family that map
    # owns pairs correctly (a shell call included) and a reasoning item riding
    # immediately before a dropped call goes with it: the Responses API rejects
    # reasoning without its required following item.
    from agents.run_internal.session_persistence import _held_items_safe_to_settle

    reasoning = cast("TResponseInputItem", {"type": "reasoning", "id": "rs_1", "summary": []})
    unpaired_shell = cast(
        "TResponseInputItem",
        {
            "type": "shell_call",
            "call_id": "sh_1",
            "id": "sh_item_1",
            "status": "completed",
            "action": {"type": "exec", "command": "ls"},
        },
    )
    paired_call: TResponseInputItem = {
        "type": "function_call",
        "call_id": "fn_1",
        "name": "write_thing",
        "arguments": "{}",
    }
    paired_output: TResponseInputItem = {
        "type": "function_call_output",
        "call_id": "fn_1",
        "output": "ok",
    }

    kept = _held_items_safe_to_settle(
        [reasoning, unpaired_shell, paired_call, paired_output], [], None
    )
    assert kept == [paired_call, paired_output]

    still_pending = _held_items_safe_to_settle(
        [reasoning, unpaired_shell], [], None, pending_call_ids={"sh_1"}
    )
    assert still_pending == [reasoning, unpaired_shell]


@pytest.mark.asyncio
async def test_settled_held_items_count_toward_the_turn_persisted_count() -> None:
    # A held batch can settle with no accompanying run items (an approval-only turn
    # converts to nothing persistable), so it lands through the original_input slot and
    # save_result_to_session returns zero new items. The settled calls are still this
    # turn's persisted items: leaving them uncounted would let a later gate-enabled
    # resume pass the resumed-safety validation with a zero count and re-append them.
    from agents.run_internal.run_steps import NextStepRunAgain
    from agents.run_internal.session_persistence import save_resumed_turn_items

    session = SimpleListSession()
    state = RunState(
        context=None,
        original_input="go",
        starting_agent=_make_deferring_agent(),
        max_turns=5,
    )
    state._current_step = NextStepRunAgain()
    held = [
        {
            "type": "function_call",
            "call_id": "call_PARKED",
            "name": "write_thing",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_PARKED", "output": "wrote:x"},
    ]

    count = await save_resumed_turn_items(
        session=session,
        items=[],
        held_input=held,  # type: ignore[arg-type]
        persisted_count=0,
        response_id=None,
        run_state=state,
    )

    assert count == 2
    assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR


@pytest.mark.asyncio
async def test_registration_forces_the_conversations_reasoning_policy() -> None:
    # A Conversations backend keeps a server-identified reasoning item persistable by
    # forcing the reasoning-id policy to None, exactly as the normal save path does; a
    # deferred registration under "omit" must match or the sanitization drops it.
    from openai.types.responses import ResponseReasoningItem
    from openai.types.responses.response_reasoning_item import Summary

    from agents.items import ReasoningItem
    from agents.run_internal.session_persistence import defer_interrupted_session_write

    agent = _make_deferring_agent()
    state = RunState(
        context=None,
        original_input="go",
        starting_agent=agent,
        max_turns=5,
    )
    state._reasoning_item_id_policy = "omit"
    reasoning = ReasoningItem(
        agent=agent,
        raw_item=ResponseReasoningItem(
            id="rs_server_1",
            summary=[Summary(text="because", type="summary_text")],
            type="reasoning",
        ),
    )

    defer_interrupted_session_write(
        state,
        _RecordingConversationsSession(),  # type: ignore[arg-type]
        run_items=[reasoning],
        reasoning_item_id_policy="omit",
    )

    pending = state._pending_session_write
    assert pending is not None
    reasoning_items = [item for item in pending["items"] if item.get("type") == "reasoning"]
    assert reasoning_items and reasoning_items[0].get("id") == "rs_server_1"


@pytest.mark.asyncio
async def test_zero_count_final_save_arms_recovery_even_when_deduplicated() -> None:
    # The zero-count branch of the final save: with guardrails the rebuilt items carry
    # the held batch, so it deduplicates out of the append, yet the append still lands
    # the approved call and output. The recovery registration must stay armed off the
    # claimed-batch flag, not the emptied payload, or a failing append loses the batch
    # with no pending record to reconcile.
    from openai.types.responses import ResponseFunctionToolCall

    from agents.items import ToolCallItem, ToolCallOutputItem
    from agents.run_internal.agent_runner_helpers import save_final_turn_items_after_guardrails

    session = _FailingResumeSession()
    agent = _make_deferring_agent()
    state = RunState(context=None, original_input="go", starting_agent=agent, max_turns=5)
    state._current_turn_persisted_item_count = 0

    call = ResponseFunctionToolCall(
        call_id="call_PARKED", name="write_thing", arguments="{}", type="function_call"
    )
    held = [
        {
            "type": "function_call",
            "call_id": "call_PARKED",
            "name": "write_thing",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_PARKED", "output": "wrote:x"},
    ]
    # The rebuilt final items already contain the held batch (guardrail rebuild), so the
    # held payload deduplicates out of the append.
    final_items = [
        ToolCallItem(agent=agent, raw_item=call),
        ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call_PARKED",
                "output": "wrote:x",
            },
            output="wrote:x",
        ),
    ]

    session.failure = "before"
    with pytest.raises(RuntimeError, match="session append failed"):
        await save_final_turn_items_after_guardrails(
            session=session,
            run_state=state,
            session_persistence_enabled=True,
            input_guardrail_results=[],
            items=final_items,
            response_id=None,
            held_input=held,  # type: ignore[arg-type]
        )

    # The append was registered before it ran, so the batch is recorded to reconcile.
    assert state._pending_session_write is not None
    assert "call_PARKED" in {i.get("call_id") for i in state._pending_session_write["items"]}


@pytest.mark.asyncio
async def test_the_settled_count_matches_what_the_append_actually_wrote() -> None:
    # The resolved turn re-delivers the very output the batch already folded in, so it
    # dedups away inside the append. Counting the batch by its raw length would report
    # more persisted items than exist, and the count slices the next save of this turn
    # positionally: an inflated count drops resolved items out of their own write.
    from agents.items import ToolCallOutputItem
    from agents.run_internal.session_persistence import save_resumed_turn_items

    agent = _make_deferring_agent()
    call: TResponseInputItem = {
        "type": "function_call",
        "call_id": "call_PARKED",
        "name": "write_thing",
        "arguments": "{}",
    }
    output: TResponseInputItem = {
        "type": "function_call_output",
        "call_id": "call_PARKED",
        "output": "wrote:x",
    }
    session = SimpleListSession()

    count = await save_resumed_turn_items(
        run_state=None,
        session=session,
        items=[ToolCallOutputItem(agent=agent, raw_item=output, output="wrote:x")],
        held_input=[call, output],
        persisted_count=0,
        response_id=None,
        reasoning_item_id_policy=None,
    )

    assert count == len(await session.get_items())


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


def _make_deferring_agent_with_a_turn_after_the_resume() -> Agent:
    """A gated write whose resume runs one more model turn before finishing.

    The extra turn moves the final output past the resumed boundary and onto the main
    loop, which owns its own detached-completion disposal.
    """
    return Agent(
        name="deferred repro (turn after resume)",
        instructions="Always call write_thing.",
        model=ScriptedModel(
            [
                ModelStep(output=[function_call("look_up", {"query": "x"}, call_id="call_LOOKUP")]),
                ModelStep(
                    output=[function_call("write_thing", {"query": "x"}, call_id="call_PARKED")]
                ),
                ModelStep(output=[function_call("look_up", {"query": "y"}, call_id="call_AFTER")]),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[look_up, write_thing],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "make_agent",
    [_make_deferring_agent, _make_deferring_agent_with_a_turn_after_the_resume],
    ids=["final-on-the-resumed-turn", "final-on-a-later-turn"],
)
async def test_a_failed_detached_completion_keeps_the_held_record(
    streamed: bool, make_agent: Callable[[], Agent]
) -> None:
    # A detached completion discards the batch because the run ends there, but only
    # once it has ended: the guardrails and the final save run after the terminal step
    # is chosen, and a failure there leaves a checkpoint whose reattach is the batch's
    # only remaining way into the Session.
    from agents import output_guardrail

    @output_guardrail
    async def _fails(ctx: Any, agent: Agent, output: Any) -> GuardrailFunctionOutput:
        raise RuntimeError("output guardrail failed")

    session = SimpleListSession()
    agent = make_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    assert state._pending_session_write is not None
    agent.output_guardrails = [*agent.output_guardrails, _fails]

    with pytest.raises(RuntimeError):
        await _run(agent, state, None, streamed=streamed)

    assert state._pending_session_write is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("parked_store", [None, False, True])
async def test_a_re_park_keeps_the_storage_setting_the_response_was_produced_under(
    parked_store: bool | None,
) -> None:
    # The batch belongs to the parked response, and the settle resolves that
    # response's compaction mode from this value. Presence decides, not truthiness: a
    # park under the ordinary ``store=None`` records a real setting, and a
    # re-interruption under a different one must not overwrite it.
    from agents.run_internal.session_persistence import defer_interrupted_session_write

    class _Session:
        session_id = "s1"

    state = object.__new__(RunState)
    state._pending_session_write = {
        "session_id": "s1",
        "items": [
            {"type": "function_call", "call_id": "call_PARKED", "name": "t", "arguments": "{}"}
        ],
        "before": None,
        "persisted_count": 1,
        "held": True,
        "response_id": "resp_parked",
        "store": parked_store,
    }
    state._current_turn_persisted_item_count = 0
    state._reasoning_item_id_policy = None

    defer_interrupted_session_write(
        state,
        _Session(),  # type: ignore[arg-type]
        run_items=[],
        reasoning_item_id_policy=None,
        response_id="resp_reinterrupted",
        store=not parked_store,
    )

    assert state._pending_session_write is not None
    assert state._pending_session_write["store"] is parked_store
    assert state._pending_session_write["response_id"] == "resp_parked"


@pytest.mark.asyncio
async def test_a_detached_re_park_folds_under_the_batch_registration_policy() -> None:
    # A Conversations-origin batch was converted preserving server reasoning ids. The
    # detached re-park cannot see the backend, so it must fold under the policy the
    # record carries rather than the resuming run's own: an id stripped here is
    # unrecoverable and the reattach would drop the reasoning item as unpersistable.
    from agents.items import ReasoningItem
    from agents.run_internal.session_persistence import extend_held_session_write

    agent = _make_deferring_agent()
    state = object.__new__(RunState)
    state._pending_session_write = {
        "session_id": "conv_abc",
        "items": [
            {"type": "function_call", "call_id": "call_PARKED", "name": "t", "arguments": "{}"}
        ],
        "before": None,
        "persisted_count": 1,
        "held": True,
        "response_id": "resp_parked",
        "store": None,
        "reasoning_item_id_policy": None,
    }
    state._current_turn_persisted_item_count = 0
    reasoning = ReasoningItem(
        agent=agent,
        raw_item={"id": "rs_SERVER_ID", "type": "reasoning", "summary": [], "content": []},
    )

    extend_held_session_write(state, run_items=[reasoning], reasoning_item_id_policy="omit")

    items = state._pending_session_write["items"]
    reasoning_ids = [i.get("id") for i in items if i.get("type") == "reasoning"]
    assert reasoning_ids == ["rs_SERVER_ID"]
    assert state._pending_session_write["reasoning_item_id_policy"] is None


@pytest.mark.asyncio
async def test_the_park_records_the_conversion_policy_it_used() -> None:
    # The record owns how its items were converted. A Conversations park forces the
    # preserving policy regardless of the run's own setting, and the recorded value is
    # what a later detached fold must reuse.
    from agents.items import ToolCallItem
    from agents.memory.openai_conversations_session import OpenAIConversationsSession
    from agents.run_internal.session_persistence import defer_interrupted_session_write

    session = object.__new__(OpenAIConversationsSession)
    session._session_id = "conv_abc"
    state = object.__new__(RunState)
    state._pending_session_write = None
    state._current_turn_persisted_item_count = 0
    call = ToolCallItem(
        agent=_make_deferring_agent(),
        raw_item={
            "type": "function_call",
            "call_id": "call_PARKED",
            "name": "t",
            "arguments": "{}",
        },
    )

    defer_interrupted_session_write(
        state,
        session,
        run_items=[call],
        reasoning_item_id_policy="omit",
        response_id="resp_parked",
        store=None,
    )

    assert state._pending_session_write is not None
    assert state._pending_session_write["reasoning_item_id_policy"] is None


class _CompactionRecordingSession(SimpleListSession):
    """Record the compaction bookkeeping a compaction-aware backend expects."""

    def __init__(self) -> None:
        super().__init__()
        self.compactions: list[dict[str, Any]] = []

    async def _defer_compaction(self, response_id: str, store: bool | None = None) -> None:
        self.compactions.append({"deferred": response_id, "store": store})

    def _get_deferred_compaction_response_id(self) -> str | None:
        return None

    async def run_compaction(self, args: Any = None) -> None:
        self.compactions.append(dict(args or {}))


@pytest.mark.asyncio
async def test_the_compaction_deferral_reads_the_settling_batch_not_the_callers_input() -> None:
    # The batch settles through ``original_input``, so the deferral has to look there;
    # but that slot also carries the caller's own turn input on every ordinary
    # interruption save. Reading the whole slot would defer compaction for a response
    # that produced no local tool output, purely because the caller resumed with an
    # earlier one in its input.
    from agents.run_internal.session_persistence import save_result_to_session

    session = _CompactionRecordingSession()
    caller_input: list[TResponseInputItem] = [
        {"type": "function_call", "call_id": "call_EARLIER", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_EARLIER", "output": "old"},
        {"role": "user", "content": "go"},
    ]

    await save_result_to_session(session, caller_input, [], None, response_id="resp_fresh")

    assert [entry for entry in session.compactions if "deferred" in entry] == []


@pytest.mark.asyncio
async def test_the_settled_count_survives_the_compaction_deferral_branch() -> None:
    # The deferral branch is the one every held settle with outputs takes on a
    # compaction-aware backend, so returning the run-item count alone there reports a
    # turn that persisted less than it wrote. That count gates the final sweep's
    # re-append protection on a later gate-enabled resume.
    from agents.run_internal.session_persistence import save_result_to_session

    session = _CompactionRecordingSession()
    held: list[TResponseInputItem] = [
        {"type": "function_call", "call_id": "call_PARKED", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_PARKED", "output": "ok"},
    ]

    count = await save_result_to_session(
        session, held, [], None, response_id="resp_parked", settling_held_batch=True
    )

    assert [entry for entry in session.compactions if "deferred" in entry] == [
        {"deferred": "resp_parked", "store": None}
    ]
    assert count == len(await session.get_items())


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_partial_settle_on_a_compaction_session_still_fails_the_gated_resume_fast(
    streamed: bool,
) -> None:
    # Park two calls, approve one, and let the gate lapse for that resume so the batch
    # settles into a compaction-aware session mid-run. Re-enable the gate and approve
    # the rest: the settled turn's count must cover what the settle wrote, or the
    # final sweep treats the turn as unpersisted and appends the stored items again.
    from agents.exceptions import UserError

    session = _CompactionRecordingSession()
    agent = _make_multi_approval_agent()

    first = await _run(agent, "go", session, streamed=streamed)
    state = await _serialized_round_trip(first, agent)
    state.approve(
        next(
            interruption
            for interruption in state.get_interruptions()
            if getattr(interruption.raw_item, "call_id", None) == "call_PARKED"
        )
    )
    gate = agent.output_guardrails
    agent.output_guardrails = []
    second = await _run(agent, state, session, streamed=streamed)
    assert len(second.interruptions) == 1
    agent.output_guardrails = gate

    state = await _serialized_round_trip(second, agent)
    for interruption in state.get_interruptions():
        state.approve(interruption)
    # The settled turn persisted items, so the re-enabled gate must refuse the resume
    # outright; an undercounted turn is what would let it proceed and re-append the
    # stored items through the final sweep.
    with pytest.raises(UserError, match="output guardrails after current-turn items"):
        await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert _call_ids(items).count("call_PARKED") == 1
    assert _call_ids(items).count("call_PARKED_2") == 1
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    # Only the second call may still be awaiting its output; nothing is duplicated.
    assert outputs == {"call_PARKED"}


@pytest.mark.asyncio
async def test_a_held_mcp_approval_pair_defers_compaction_when_it_settles() -> None:
    # The approval response is the locally produced half of its pair and must stay
    # associated with the response chain that carried the request; compacting that
    # response before the model consumes the approval drops it in
    # ``previous_response_id`` mode.
    from agents.run_internal.session_persistence import save_result_to_session

    session = _CompactionRecordingSession()
    held: list[TResponseInputItem] = [
        {
            "type": "mcp_approval_request",
            "id": "mcpr_1",
            "server_label": "srv",
            "name": "do_it",
            "arguments": "{}",
        },
        {"type": "mcp_approval_response", "approval_request_id": "mcpr_1", "approve": True},
    ]

    count = await save_result_to_session(
        session, held, [], None, response_id="resp_parked", settling_held_batch=True
    )

    assert [entry for entry in session.compactions if "deferred" in entry] == [
        {"deferred": "resp_parked", "store": None}
    ]
    assert [entry for entry in session.compactions if "response_id" in entry] == []
    assert count == len(await session.get_items())


@pytest.mark.asyncio
async def test_an_ordinary_mcp_approval_response_defers_compaction_too() -> None:
    # The non-deferred resume commits the approval response as a run item, and the
    # classification must treat both carriers alike: deferring for the settled dict
    # but not for the run item would leave the same response compacted or not
    # depending on which path persisted it.
    from agents.items import MCPApprovalResponseItem
    from agents.run_internal.session_persistence import save_result_to_session

    session = _CompactionRecordingSession()
    agent = _make_deferring_agent()
    response_item = MCPApprovalResponseItem(
        agent=agent,
        raw_item={
            "type": "mcp_approval_response",
            "approval_request_id": "mcpr_1",
            "approve": True,
        },
    )

    await save_result_to_session(session, [], [response_item], None, response_id="resp_live")

    assert [entry for entry in session.compactions if "deferred" in entry] == [
        {"deferred": "resp_live", "store": None}
    ]


@pytest.mark.asyncio
async def test_the_entry_settle_runs_the_compaction_bookkeeping() -> None:
    # The entry settle goes through the canonical persistence path, so a
    # compaction-aware backend still gets the bookkeeping for the response the held
    # batch belongs to. Appending behind that path would silently skip a supported
    # compaction hook for the interrupted response.
    from agents.run_internal.run_steps import NextStepRunAgain
    from agents.run_internal.session_persistence import resume_pending_session_write

    session = _CompactionRecordingSession()
    state = RunState(
        context=None,
        original_input="go",
        starting_agent=_make_deferring_agent(),
        max_turns=5,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": "test",
        "items": [
            {
                "type": "function_call",
                "call_id": "call_PARKED",
                "name": "write_thing",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_PARKED", "output": "wrote:x"},
        ],
        "before": None,
        "persisted_count": 2,
        "held": True,
        "response_id": "resp_parked",
    }

    await resume_pending_session_write(state, session)  # type: ignore[arg-type]

    assert state._pending_session_write is None
    assert _parked_pair(await session.get_items()) == _EXPECTED_PAIR
    # The batch carries the approved tool's output, so this response's compaction must
    # be DEFERRED, not run: compacting it here would discard the very output that just
    # landed. Asserting the specific hook is the point; "some hook fired" would pass
    # either way.
    assert session.compactions == [{"deferred": "resp_parked", "store": None}], (
        f"expected a deferred compaction for the parked response, got {session.compactions}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_the_park_records_the_response_the_batch_belongs_to(streamed: bool) -> None:
    # The settle runs the compaction bookkeeping for the response the withheld batch
    # came from, so the park has to record which response that was.
    session = SimpleListSession()
    agent = _make_deferring_agent()

    first = await _run(agent, "do the thing", session, streamed=streamed)
    assert len(first.interruptions) == 1

    pending = first.to_state().to_json()["pending_session_write"]
    assert pending["held"] is True
    assert pending["response_id"] == first.raw_responses[-1].response_id


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
    state = await _parked_and_approved(agent, session, streamed=streamed)

    with pytest.raises(Exception, match="extractor boom"):
        await _run(agent, state, session, streamed=streamed)

    pending = state._pending_session_write
    assert pending is not None
    assert _parked_pair(pending["items"]) == _EXPECTED_PAIR


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
async def test_the_park_records_the_store_the_response_was_produced_under(
    streamed: bool,
) -> None:
    # The settle defers compaction for the parked response, and the deferral resolves a
    # compaction mode from the store setting. That setting belongs to the turn the
    # batch was withheld in, not to the resume, so the park records it.
    session = SimpleListSession()
    agent = _make_deferring_agent()
    agent.model_settings = replace(agent.model_settings, store=True)

    first = await _run(agent, "do the thing", session, streamed=streamed)
    assert len(first.interruptions) == 1

    pending = first.to_state().to_json()["pending_session_write"]
    assert pending["held"] is True
    assert pending["store"] is True


@pytest.mark.asyncio
async def test_the_entry_settle_defers_with_the_recorded_store() -> None:
    # The recorded store reaches the deferral, so the hook resolves the same compaction
    # mode the ordinary persistence path would have resolved for that response.
    from agents.run_internal.run_steps import NextStepRunAgain
    from agents.run_internal.session_persistence import resume_pending_session_write

    session = _CompactionRecordingSession()
    state = RunState(
        context=None,
        original_input="go",
        starting_agent=_make_deferring_agent(),
        max_turns=5,
    )
    state._current_step = NextStepRunAgain()
    state._pending_session_write = {
        "session_id": "test",
        "items": [
            {
                "type": "function_call",
                "call_id": "call_PARKED",
                "name": "write_thing",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_PARKED", "output": "wrote:x"},
        ],
        "before": None,
        "persisted_count": 2,
        "held": True,
        "response_id": "resp_parked",
        "store": True,
    }

    await resume_pending_session_write(state, session)  # type: ignore[arg-type]

    assert session.compactions == [{"deferred": "resp_parked", "store": True}]
