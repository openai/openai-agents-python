from __future__ import annotations

import json
from typing import Any, Literal

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
async def test_an_emptied_resolved_turn_corrupts_nothing_in_either_runner(
    streamed: bool,
) -> None:
    # When a handoff input_filter empties the resolved turn, the held batch must not be
    # written on its own: a call with no output poisons the Session exactly as the
    # orphaned output does.
    session = SimpleListSession()
    agent = _make_emptying_handoff_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    resumed = await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
    assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"
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
