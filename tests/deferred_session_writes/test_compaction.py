from __future__ import annotations

from typing import Any

import pytest

from agents import (
    RunState,
)
from agents.items import TResponseInputItem
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _EXPECTED_PAIR,
    _call_ids,
    _held_write,
    _make_deferring_agent,
    _make_multi_approval_agent,
    _parked_pair,
    _run,
    _serialized_round_trip,
)


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
async def test_the_final_sweep_settle_defers_compaction_and_counts_what_it_wrote() -> None:
    # A reattached detached carry can reach the final exit with a zero persisted
    # count, so the batch settles through the final sweep's direct save. That save
    # must speak the same settle dialect as every other one: the deferral must see
    # the batch's outputs even when the final turn carries none of its own, and the
    # returned count must cover what the append actually wrote.
    from agents.items import MessageOutputItem
    from agents.run_internal.agent_runner_helpers import save_final_turn_items_after_guardrails
    from agents.testing.model import assistant_message

    session = _CompactionRecordingSession()
    agent = _make_deferring_agent()
    state = object.__new__(RunState)
    state._pending_session_write = None
    state._current_turn_persisted_item_count = 0
    state._reasoning_item_id_policy = None
    state._current_step = None
    state._current_turn = 0
    held: list[TResponseInputItem] = [
        {"type": "function_call", "call_id": "call_PARKED", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_PARKED", "output": "ok"},
    ]

    state._pending_session_write = _held_write(held)
    count = await save_final_turn_items_after_guardrails(
        session=session,
        run_state=state,
        session_persistence_enabled=True,
        input_guardrail_results=[],
        items=[MessageOutputItem(agent=agent, raw_item=assistant_message("done"))],
        response_id="resp_final",
    )

    assert [entry for entry in session.compactions if "deferred" in entry] == [
        {"deferred": "resp_final", "store": None}
    ]
    assert count == len(await session.get_items())


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
