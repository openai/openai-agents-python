from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from agents import (
    Agent,
    HandoffInputData,
    Runner,
    RunState,
    StopAtTools,
    function_tool,
    handoff,
)
from agents.exceptions import OutputGuardrailTripwireTriggered
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _DEFERRING_BEHAVIOR,
    _EXPECTED_PAIR,
    _call_ids,
    _make_deferring_agent,
    _make_partial_filter_handoff_agent,
    _make_terminal_tool_agent,
    _make_two_park_agent,
    _orphaned_outputs,
    _parked_and_approved,
    _parked_pair,
    _run,
    _serialized_round_trip,
    always_crashes,
    always_fine,
    always_trips,
    look_up,
    write_thing,
)


@function_tool(needs_approval=True)
async def read_secret(query: str) -> str:
    return "SECRET-VALUE-42"


def _make_secret_handoff_agent(input_filter: Any) -> Agent:
    """A gated secret-bearing tool resolved into a handoff with the given filter."""
    from agents import handoff

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel([ModelStep(output=[assistant_message("done")])]),
    )
    return Agent(
        name="deferred repro (secret)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("read_secret", {"query": "x"}, call_id="call_SECRET"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[read_secret],
        handoffs=[handoff(target, input_filter=input_filter)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_an_emptied_resolved_turn_honors_the_filters_history_authority(
    streamed: bool,
) -> None:
    # ``HandoffInputData.new_items`` is the session-history axis by contract, so a
    # filter that empties it is asking for nothing of this turn to persist. The
    # approved tool ran, but persisting its pair from the held record would defeat the
    # documented filter contract; callers who want the record keep ``new_items`` and
    # filter model input through ``input_items`` instead.
    session = SimpleListSession()
    agent = _make_emptying_handoff_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    resumed = await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"
    assert outputs - calls == set(), f"orphaned outputs: {sorted(map(str, outputs - calls))}"
    assert "call_PARKED" not in calls, "the filter removed the pair from session history"
    assert "call_HANDOFF" not in calls
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
    assert "call_PARKED_2" not in calls, "the filtered output must not settle from the batch"
    assert "pending_session_write" not in resumed.to_state().to_json()
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_the_librarys_own_filter_keeps_the_secret_out_of_the_session(
    streamed: bool,
) -> None:
    # ``remove_all_tools`` filters both ``new_items`` and ``input_items``: it wants
    # tool data out of session history too. The held settle must not put back what the
    # library's own filter removed.
    from agents.extensions.handoff_filters import remove_all_tools

    session = SimpleListSession()
    agent = _make_secret_handoff_agent(remove_all_tools)
    state = await _parked_and_approved(agent, session, streamed=streamed)
    await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert not any("SECRET-VALUE-42" in json.dumps(item) for item in items)
    assert _orphaned_outputs(items) == []
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert calls - outputs == set(), f"dangling calls: {sorted(map(str, calls - outputs))}"


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_an_input_items_only_filter_preserves_the_pair_in_session(
    streamed: bool,
) -> None:
    # ``input_items`` is the model-input axis: filtering it says nothing about session
    # history, so the executed pair persists exactly as an unfiltered handoff would.

    def input_only(data: HandoffInputData) -> HandoffInputData:
        return data.clone(input_items=())

    session = SimpleListSession()
    agent = _make_secret_handoff_agent(input_only)
    state = await _parked_and_approved(agent, session, streamed=streamed)
    await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert any("SECRET-VALUE-42" in json.dumps(item) for item in items)
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert "call_SECRET" in calls and "call_SECRET" in outputs
    assert calls - outputs == set()


@pytest.mark.asyncio
async def test_a_detached_filtered_handoff_drops_the_pair_before_the_reattach() -> None:
    # The filter's authority does not lapse because the resume ran detached: the fold
    # happened in this process, so the exit can still tell a current-turn output from
    # carried history, and the batch must not smuggle the filtered pair to the
    # reattaching entry settle. Cancellation only exists on the streaming runner.
    from agents.extensions.handoff_filters import remove_all_tools

    session = SimpleListSession()
    agent = _make_secret_handoff_agent(remove_all_tools)
    state = await _parked_and_approved(agent, session, streamed=True)

    detached = Runner.run_streamed(agent, state, session=None)
    detached.cancel(mode="after_turn")
    async for _ in detached.stream_events():
        pass

    checkpoint = detached.to_state().to_json()
    pending = checkpoint.get("pending_session_write")
    assert pending is not None, "the after-turn cancel must leave the batch riding"
    assert not any("SECRET-VALUE-42" in json.dumps(item) for item in pending["items"]), (
        "the filtered secret must not ride the checkpoint to the reattach"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_a_carried_pair_survives_a_filtered_handoff_on_a_later_run(
    streamed: bool,
) -> None:
    # After a checkpoint the folded set is empty on purpose: an output folded by an
    # earlier process is carried prior-turn history, which a later turn's filter is
    # not entitled to remove, exactly as the eager path cannot unpersist earlier
    # turns. The reattaching entry settle keeps the carried batch whole.
    session = SimpleListSession()
    agent = _make_deferring_agent()
    state = await _parked_and_approved(agent, session, streamed=True)

    detached = Runner.run_streamed(agent, state, session=None)
    detached.cancel(mode="after_turn")
    async for _ in detached.stream_events():
        pass
    checkpoint = detached.to_state().to_json()
    pending = checkpoint.get("pending_session_write")
    assert pending is not None
    assert {item.get("type") for item in pending["items"]} >= {
        "function_call",
        "function_call_output",
    }

    state = await RunState.from_json(agent, json.loads(json.dumps(checkpoint)))
    reattached = await _run(agent, state, session, streamed=streamed)
    assert reattached.final_output == "done"
    items = await session.get_items()
    assert _parked_pair(items) == _EXPECTED_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_a_filtered_unkeyed_sibling_stays_out_of_the_session(
    streamed: bool, approved: bool
) -> None:
    # The batch carries the parked response's unkeyed companions (an assistant
    # preamble, an id-less reasoning item), and the filter's authority covers them
    # exactly as it covers the outputs: removed from the view means removed from
    # session history, keyed or not.
    from agents import HandoffInputData, handoff

    def drops_preamble(data: HandoffInputData) -> HandoffInputData:
        def keep(items: tuple) -> tuple:
            return tuple(item for item in items if item.type != "message_output_item")

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
    agent = Agent(
        name="deferred repro (unkeyed sibling)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        assistant_message("PREAMBLE-THE-FILTER-REMOVED"),
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing],
        handoffs=[handoff(target, input_filter=drops_preamble)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )
    session = SimpleListSession()
    parked = await _run(agent, "go", session, streamed=streamed)
    state = await _serialized_round_trip(parked, agent)
    interruption = state.get_interruptions()[0]
    if approved:
        state.approve(interruption)
    else:
        state.reject(interruption)
    await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert not any("PREAMBLE-THE-FILTER-REMOVED" in json.dumps(item) for item in items)
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert "call_PARKED" in calls and "call_PARKED" in outputs
    assert calls - outputs == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_handoff_filter_applies_to_sibling_completed_before_approval(streamed: bool) -> None:
    from agents import handoff
    from agents.extensions.handoff_filters import remove_all_tools

    target = Agent(
        name="target", model=ScriptedModel([ModelStep(output=[assistant_message("done")])])
    )
    agent = Agent(
        name="source",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call("look_up", {"query": "x"}, call_id="call_LOOKUP"),
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
            ]
        ),
        tools=[look_up, write_thing],
        handoffs=[handoff(target, input_filter=remove_all_tools)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )
    session = SimpleListSession()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    result = await _run(agent, state, session, streamed=streamed)
    assert result.final_output == "done"
    assert not any(
        item.get("type") in {"function_call", "function_call_output"}
        for item in await session.get_items()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_detached_terminal_guardrail_failure_keeps_unsettled_checkpoint(
    streamed: bool,
) -> None:
    from agents.exceptions import UserError

    session = SimpleListSession()
    agent = _make_terminal_tool_agent()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    agent.output_guardrails = [always_crashes]
    with pytest.raises(RuntimeError, match="guardrail crashed"):
        await _run(agent, state, None, streamed=streamed)

    assert state._pending_session_write is not None
    agent.output_guardrails = [always_fine]
    # A terminal side effect without a completed save must fail closed on reload,
    # rather than allowing a retry that forgets the executed tool.
    with pytest.raises(UserError, match="pending Session write is invalid"):
        await RunState.from_json(agent, state.to_json())
    assert _parked_pair(await session.get_items()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_later_tripwire_preserves_accepted_detached_history(streamed: bool) -> None:
    agent = _make_two_park_agent()
    agent.tool_use_behavior = StopAtTools(stop_at_tool_names=["write_other"])
    session = SimpleListSession()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    detached = await _run(agent, state, None, streamed=streamed)
    state = await _serialized_round_trip(detached, agent)
    state.approve(state.get_interruptions()[0])
    agent.output_guardrails = [always_trips]

    with pytest.raises(OutputGuardrailTripwireTriggered):
        await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert _call_ids(items) == ["call_A", "call_B"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert [item.get("call_id") for item in outputs] == ["call_A", "call_B"]
    assert outputs[0]["output"] == "wrote:a"
    assert "other:b" not in json.dumps(items)
    assert state._pending_session_write is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_detached_repark_terminal_settle_preserves_each_response_once(
    streamed: bool,
) -> None:
    agent = _make_two_park_agent()
    agent.tool_use_behavior = StopAtTools(stop_at_tool_names=["write_other"])
    agent.model = ScriptedModel(
        [
            ModelStep(
                output=[
                    assistant_message("FIRST-PREAMBLE"),
                    function_call("write_thing", {"query": "a"}, call_id="call_A"),
                ]
            ),
            ModelStep(
                output=[
                    assistant_message("SECOND-PREAMBLE"),
                    function_call("write_other", {"query": "b"}, call_id="call_B"),
                ]
            ),
        ]
    )
    session = SimpleListSession()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    detached = await _run(agent, state, None, streamed=streamed)
    state = await _serialized_round_trip(detached, agent)
    state.approve(state.get_interruptions()[0])
    result = await _run(agent, state, session, streamed=streamed)
    assert result.final_output == "other:b"
    history = await session.get_items()
    for text in ("FIRST-PREAMBLE", "SECOND-PREAMBLE"):
        assert sum(text in json.dumps(item) for item in history) == 1
    assert _call_ids(history) == ["call_A", "call_B"]
    assert _orphaned_outputs(history) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_an_additive_filter_keeps_the_batchs_companions(streamed: bool) -> None:
    # A filter that only appends removed nothing, so the parked response's unkeyed
    # companions must persist: their absence from the resolved items says nothing,
    # because they ride the filtered pre-step view, and only absence from the whole
    # filtered view is the filter's verdict.
    from agents import handoff
    from agents.items import MessageOutputItem

    def additive(data: HandoffInputData) -> HandoffInputData:
        injected = MessageOutputItem(
            agent=Agent(name="filler", instructions="x"),
            raw_item=assistant_message("INJECTED-BY-FILTER"),
        )
        return data.clone(new_items=(*data.new_items, injected))

    target = Agent(
        name="target",
        instructions="x",
        model=ScriptedModel([ModelStep(output=[assistant_message("done")])]),
    )
    agent = Agent(
        name="deferred repro (additive filter)",
        instructions="x",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        assistant_message("COMPANION-KEPT-BY-FILTER"),
                        function_call("write_thing", {"query": "x"}, call_id="call_PARKED"),
                        function_call("transfer_to_target", {}, call_id="call_HANDOFF"),
                    ]
                ),
                ModelStep(output=[assistant_message("done")]),
            ]
        ),
        tools=[write_thing],
        handoffs=[handoff(target, input_filter=additive)],
        output_guardrails=[always_fine],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
    )
    session = SimpleListSession()
    state = await _parked_and_approved(agent, session, streamed=streamed)
    await _run(agent, state, session, streamed=streamed)

    items = await session.get_items()
    assert any("COMPANION-KEPT-BY-FILTER" in json.dumps(item) for item in items)
    calls = set(_call_ids(items))
    outputs = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    assert "call_PARKED" in calls and "call_PARKED" in outputs


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "prior_text,current_count,removed_index,expected_count",
    [
        ("SAME PREAMBLE", 1, -1, 1),
        ("SAME PREAMBLE", 1, 0, 2),
        ("EARLIER PREAMBLE", 2, -1, 1),
    ],
)
async def test_handoff_filter_distinguishes_equal_preambles(
    streamed: bool, prior_text: str, current_count: int, removed_index: int, expected_count: int
) -> None:
    def filter_preamble(data: HandoffInputData) -> HandoffInputData:
        messages = [
            i for i, item in enumerate(data.pre_handoff_items) if item.type == "message_output_item"
        ]
        assert len(messages) == 1 + current_count
        removed = messages[removed_index]
        return data.clone(
            pre_handoff_items=tuple(
                copy.deepcopy(item) for i, item in enumerate(data.pre_handoff_items) if i != removed
            )
        )

    target = Agent(
        name="target", model=ScriptedModel([ModelStep(output=[assistant_message("done")])])
    )
    agent = Agent(
        name="source",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        assistant_message(prior_text),
                        function_call("look_up", {"query": "x"}, call_id="lookup"),
                    ]
                ),
                ModelStep(
                    output=[
                        *[assistant_message("SAME PREAMBLE") for _ in range(current_count)],
                        function_call("write_thing", {"query": "x"}, call_id="write"),
                        function_call("transfer_to_target", {}, call_id="handoff"),
                    ]
                ),
            ]
        ),
        tools=[look_up, write_thing],
        handoffs=[handoff(target, input_filter=filter_preamble)],
        tool_use_behavior=_DEFERRING_BEHAVIOR,
        output_guardrails=[always_fine],
    )
    session = SimpleListSession()
    parked = await _run(agent, "go", session, streamed=streamed)
    assert sum(prior_text in json.dumps(item) for item in await session.get_items()) == 1
    state = await _serialized_round_trip(parked, agent)
    state.approve(state.get_interruptions()[0])
    await _run(agent, state, session, streamed=streamed)
    # Keep accepted prior history, and only the current occurrences the filter kept.
    # Equal text in either an earlier response or this response cannot restore a removal.
    assert (
        sum("SAME PREAMBLE" in json.dumps(item) for item in await session.get_items())
        == expected_count
    )
