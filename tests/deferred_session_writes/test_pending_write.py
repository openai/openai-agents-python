from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from agents import (
    RunState,
)
from agents.items import TResponseInputItem
from agents.memory.openai_conversations_session import OpenAIConversationsSession
from tests.utils.simple_session import SimpleListSession

from .helpers import (
    _EXPECTED_PAIR,
    _FailingResumeSession,
    _held_write,
    _make_deferring_agent,
    _parked_pair,
    _RecordingConversationsSession,
    _run,
)


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
        held_write=_held_write(held),
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
    state._pending_session_write = _held_write(held)
    with pytest.raises(RuntimeError, match="session append failed"):
        await save_final_turn_items_after_guardrails(
            session=session,
            run_state=state,
            session_persistence_enabled=True,
            input_guardrail_results=[],
            items=final_items,
            response_id=None,
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
        held_write=_held_write([call, output]),
        persisted_count=0,
        response_id=None,
        reasoning_item_id_policy=None,
    )

    assert count == len(await session.get_items())


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
