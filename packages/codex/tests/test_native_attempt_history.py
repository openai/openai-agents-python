"""Recorded native usage replaces cumulative snapshots once per physical owner."""

import json

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
