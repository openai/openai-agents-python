from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    RunState,
    output_guardrail,
)
from agents.exceptions import OutputGuardrailTripwireTriggered
from agents.items import TResponseInputItem
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _DEFERRING_BEHAVIOR,
    _EXPECTED_PAIR,
    _PREAMBLE_TEXT,
    _call_ids,
    _make_deferring_agent,
    _make_multi_approval_agent,
    _make_partial_filter_handoff_agent,
    _make_terminal_tool_agent,
    _make_two_park_agent,
    _orphaned_outputs,
    _parked_and_approved,
    _parked_pair,
    _run,
    _serialized_round_trip,
    always_fine,
    look_up,
    write_thing,
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


class _AppendRecordingSession(SimpleListSession):
    """Record each ``add_items`` batch to observe write ordering and granularity."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[TResponseInputItem]] = []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.batches.append(list(items))
        await super().add_items(items)


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
    # The filter kept this pair, so the batch must deliver it to the reattach; losing
    # it silently would look symmetric too.
    assert "call_PARKED" in calls and "call_PARKED" in outputs


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
