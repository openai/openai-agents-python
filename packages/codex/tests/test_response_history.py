"""Received response facts and legacy accounting occurrences share one history."""

from __future__ import annotations

from typing import Any

import pytest

from agents._history import project_history
from agents.history import RecordedTurn
from agents.state import SessionState


def accounting(link: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "thread_id": "origin-thread",
        "turn_id": "turn",
        "response_id": "reused-provider-id",
        "usage": {"input_tokens": 7},
        "turn_token_usage": {"input_tokens": 14},
    }
    if link is not None:
        payload["response_observation_id"] = link
    return {"type": "token_usage_record", "payload": payload}


def observation(
    identity: str = "observation-a", usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "type": "response_observation_record",
        "payload": {
            "id": identity,
            "thread_id": "origin-thread",
            "turn_id": "turn",
            "response_id": "reused-provider-id",
            "status": "incomplete",
            "incomplete_reason": "max_output_tokens",
            "usage": usage,
        },
    }


def project(*records: dict[str, Any]) -> RecordedTurn:
    turns, _ = project_history(
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            *records,
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
        ],
        "current-session",
    )
    return turns[0]


@pytest.mark.parametrize(
    ("records", "ids", "sources"),
    [
        ([accounting(), accounting()], [None, None], ["native_normalized"] * 2),
        (
            [accounting(), observation(), accounting("observation-a")],
            [None, "observation-a"],
            ["native_normalized", "received"],
        ),
        ([observation(), accounting("observation-a")], ["observation-a"], ["received"]),
        (
            [accounting("observation-a"), observation()],
            ["observation-a"],
            ["received"],
        ),
        ([accounting("missing-observation")], ["missing-observation"], ["native_normalized"]),
        (
            [observation(), observation("observation-b")],
            ["observation-a", "observation-b"],
            ["received"] * 2,
        ),
        ([observation(), observation()], ["observation-a"], ["received"]),
    ],
)
def test_projection_matches_occurrences_not_provider_ids(
    records: list[dict[str, Any]], ids: list[str | None], sources: list[str]
) -> None:
    turn = project(*records)
    assert [response["id"] for response in turn["responses"]] == ids
    assert [response["usage_source"] for response in turn["responses"]] == sources
    assert all(response["thread_id"] == "origin-thread" for response in turn["responses"])
    assert turn["session_id"] == "current-session"
    for response in turn["responses"]:
        if response["usage_source"] == "native_normalized":
            assert "status" not in response
            assert "incomplete_reason" not in response
            assert response["usage"] is not None
            assert response["usage"]["input_tokens"] == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ({}, {}),
        ({"cached_input_tokens": 0}, {"input_tokens_details": {"cached_tokens": 0}}),
        (
            {"cache_write_input_tokens": 0, "reasoning_output_tokens": 0},
            {
                "input_tokens_details": {"cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        ),
        (
            {"input_tokens": 2**60 + 1, "total_tokens": None},
            {"input_tokens": 2**60 + 1},
        ),
    ],
)
def test_received_usage_keeps_unknown_zero_and_exact_counts(
    raw: dict[str, Any] | None, expected: dict[str, Any] | None
) -> None:
    turn = project(observation(usage=raw))
    response = turn["responses"][0]
    assert response["usage"] == expected
    assert response["status"] == "incomplete"
    assert response["incomplete_reason"] == "max_output_tokens"
    assert turn["usage"] is None
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="current-session",
        history=[],
        root_metadata={},
        repair_turns=[turn],
    )
    assert SessionState.from_json(state.to_json()).repair_turns == [turn]


def test_legacy_receipt_and_missing_response_list_remain_readable() -> None:
    turn = project()
    assert "responses" not in turn
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="current-session",
        history=[],
        root_metadata={},
        repair_turns=[turn],
    )
    assert SessionState.from_json(state.to_json()).repair_turns == [turn]


@pytest.mark.parametrize("reported", [None, 0, 4])
@pytest.mark.parametrize("mixed", [False, True])
def test_aggregate_cache_writes_preserve_linked_unknowns_and_reported_values(
    reported: int | None, mixed: bool
) -> None:
    records = []
    if mixed:
        records.extend([observation("known", {"cache_write_input_tokens": 4}), accounting("known")])
    raw = {} if reported is None else {"cache_write_input_tokens": reported}
    record = accounting("observation-a")
    total = (4 if mixed else 0) + (reported or 0)
    record["payload"]["turn_token_usage"]["cache_write_input_tokens"] = total
    records.extend([observation(usage=raw), record])
    turn = project(*records)
    assert turn["usage"] is not None
    details = turn["usage"]["input_tokens_details"]
    assert details.get("cache_write_tokens") == (None if reported is None else total)
    assert details["cached_tokens"] == 0
    assert turn["usage"]["input_tokens"] == 14
    assert turn["usage"]["output_tokens_details"]["reasoning_tokens"] == 0
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="current-session",
        history=[
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            *records,
        ],
        root_metadata={},
    )
    restored = SessionState.from_json(state.to_json())
    assert project(*restored.history)["usage"] == turn["usage"]


@pytest.mark.parametrize("mismatch", ["unlinked", "missing", "thread_id", "turn_id"])
def test_aggregate_cache_writes_require_exact_contributor_link(mismatch: str) -> None:
    received = observation(usage={})
    record = accounting(None if mismatch == "unlinked" else "observation-a")
    record["payload"]["turn_token_usage"]["cache_write_input_tokens"] = 0
    if mismatch == "missing":
        record["payload"]["response_observation_id"] = "missing"
    elif mismatch in {"thread_id", "turn_id"}:
        received["payload"][mismatch] = "different-origin"
    turn = project(received, record)
    assert turn["usage"] is not None
    assert turn["usage"]["input_tokens_details"]["cache_write_tokens"] == 0


def test_rollback_discards_response_projection_indexes() -> None:
    turn = project(
        observation(),
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        accounting("observation-a"),
    )
    assert len(turn["responses"]) == 1
    assert turn["responses"][0]["usage_source"] == "native_normalized"


def test_compaction_resume_metadata_does_not_add_a_response_occurrence() -> None:
    turn = project(
        observation(),
        accounting("observation-a"),
        {"type": "compacted", "payload": {"latest_token_usage_record": accounting()["payload"]}},
    )
    assert len(turn["responses"]) == 1
    assert turn["usage"] is not None
    assert turn["usage"]["input_tokens"] == 14


@pytest.mark.parametrize("response_id", [None, ""])
def test_unknown_or_empty_provider_id_does_not_replace_native_observation_id(
    response_id: str | None,
) -> None:
    record = observation()
    record["payload"]["response_id"] = response_id
    response = project(record)["responses"][0]
    assert response["response_id"] == response_id
    assert response["id"] == "observation-a"


@pytest.mark.parametrize("sampled_receipt", [False, True])
def test_sampled_receipt_response_facts_and_late_execution_coexist(
    sampled_receipt: bool,
) -> None:
    context: dict[str, Any] = {"turn_id": "turn"}
    if sampled_receipt:
        context["sdk_receipt"] = {
            "version": 1,
            "thread_id": "origin-thread",
            "model": "sampled-model",
            "reasoning_effort": None,
            "instruction_revision": None,
            "developer_instructions_sha256": None,
            "instruction_files": [],
            "user_config_applied": False,
            "codex_revision": "synthetic-revision",
            "codex_source_tree": "synthetic-source-tree",
            "approval_policy": "never",
            "executors": None,
        }
    turns, items = project_history(
        [
            {"type": "turn_context", "payload": context},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            observation(usage={"input_tokens": -1, "cached_input_tokens": 0}),
            accounting("observation-a"),
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "origin-thread",
                    "turn_id": "turn",
                    "item": {
                        "type": "McpToolCall",
                        "id": "execution-call",
                        "server": "fixture",
                        "tool": "read",
                        "arguments": {},
                        "result": {"content": []},
                        "status": "completed",
                    },
                },
            },
        ],
        "current-session",
    )
    [turn] = turns
    assert turn["status"] == "completed"
    assert turn["session_id"] == "current-session"
    assert ("receipt" in turn) is sampled_receipt
    if sampled_receipt:
        assert turn["receipt"]["thread_id"] == "origin-thread"
        assert turn["receipt"]["turn_id"] == "turn"
        assert turn["receipt"]["model"] == "sampled-model"
    [response] = turn["responses"]
    assert response["id"] == "observation-a"
    assert response["thread_id"] == "origin-thread"
    assert response["status"] == "incomplete"
    assert response["usage_source"] == "received"
    assert response["usage"] == {"input_tokens": -1, "input_tokens_details": {"cached_tokens": 0}}
    [execution] = [item for item in items if item["type"] == "mcp_call"]
    assert execution["thread_id"] == "origin-thread"
    assert execution["turn_id"] == "turn"
    assert execution["call_id"] == "execution-call"
    assert execution["status"] == "completed"
