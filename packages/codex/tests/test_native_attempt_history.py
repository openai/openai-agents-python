"""Recorded native usage replaces cumulative snapshots once per physical owner."""

import asyncio
import json

import pytest

from _agents_codex._history import project_history


def event(kind, turn="turn", **fields):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **fields}}


def fact(owner, requests, tokens, missing=0, turn="turn"):
    return event(
        "sdk_model_attempt",
        turn,
        thread="original-thread",
        data={
            "attempt_id": "attempt",
            "phase": "finished",
            "owner_attempt_id": owner,
            "terminal_attempt_id": None,
            "usage": {
                "requests": requests,
                "requests_without_token_usage": missing,
                "token_usage": {
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": tokens,
                },
            },
        },
    )


def raw(tokens, *, attempt=None, turn="turn"):
    return event(
        "raw_response_completed",
        turn,
        token_usage={
            "input_tokens": tokens,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": tokens,
        },
        **({"model_attempt_id": attempt} if attempt else {}),
    )


def test_legacy_history_without_native_attempt_facts_keeps_its_existing_record_shape():
    turns, _ = project_history([event("turn_started"), raw(5), event("turn_complete")], "session")
    assert len(turns) == 1 and "run_usage" not in turns[0]
    assert turns[0]["usage"]["input_tokens"] == 5


def test_latest_owner_snapshots_preserve_legacy_and_compaction_without_counting_raw_copies():
    records = [
        event("turn_started"),
        raw(5),
        fact("owner-A", 1, 0, 1),
        raw(100, attempt="generation-A"),
        fact("owner-A", 2, 7, 1),
        fact("owner-A", 2, 7, 1),
        fact("restored-owner-B", 1, 11),
        raw(3),
        event("turn_complete"),
    ]
    turns, _ = project_history(records, "queried-fork")
    usage = turns[0]["run_usage"]
    assert usage["input_tokens"] == 26 and usage["total_tokens"] == 26
    assert usage["requests"] == 5 and usage["missing_usage_requests"] == 1
    assert turns[0]["usage"]["input_tokens"] == 108
    assert json.loads(json.dumps(turns))[0]["run_usage"] == usage


def test_rollback_removes_old_owner_and_legacy_contributions_before_same_id_recovery():
    records = [
        event("turn_started"),
        raw(5),
        fact("removed-owner", 2, 7),
        event("turn_complete"),
        event("thread_rolled_back", num_turns=1),
        event("turn_started"),
        fact("fresh-owner", 1, 3),
        event("turn_complete"),
    ]
    turns, _ = project_history(records, "session")
    assert len(turns) == 1
    assert turns[0]["run_usage"]["requests"] == 1
    assert turns[0]["run_usage"]["input_tokens"] == 3


@pytest.mark.parametrize("terminal", ["task_complete", "turn_complete", "turn_aborted"])
def test_terminal_snapshots_replace_the_matching_owner_without_double_counting(terminal):
    snapshot = fact("owner-A", 1, 7)["payload"]["data"]
    records = [
        event("turn_started"),
        fact("restored-owner", 1, 3),
        fact("owner-A", 1, 0, 1),
        raw(7, attempt="attempt"),
        event(terminal, model_attempt=snapshot),
    ]
    turns, _ = project_history(records, "session")
    assert turns[0]["run_usage"]["input_tokens"] == 10
    assert turns[0]["run_usage"]["requests"] == 2
    assert turns[0]["run_usage"]["missing_usage_requests"] == 0


async def test_force_cancel_retains_reported_model_usage_in_recorded_turns():
    from agents import Agent, ModelRequest, NativeProvider, TurnStoppedError
    from tests._asyncio import timeout

    reported, released = asyncio.Event(), asyncio.Event()
    reports = []

    class Model:
        async def complete(self, request: ModelRequest):
            reports.append(request.report_usage({"input_tokens": 7, "total_tokens": 7}))
            reported.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(model="custom-model", instructions="Answer.", provider=provider)
        async with agent.session() as session:
            turn = await session.submit("go")
            await reported.wait()
            await turn.cancel(mode="force")
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            assert released.is_set() and reports == [True]
            recorded = [item async for item in session.turns()]
            assert len(recorded) == 1 and recorded[0]["id"] == turn.turn_id
            usage = recorded[0]["run_usage"]
            assert usage["input_tokens"] == usage["total_tokens"] == 7
            assert usage["requests"] == 1 and usage["missing_usage_requests"] == 0
