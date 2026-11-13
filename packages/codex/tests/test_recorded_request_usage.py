"""Recorded attempt detail requires contiguous authoritative settled facts."""

from _agents_codex._history import project_history
from tests.test_native_attempt_history import event, fact, raw


def attempt(owner, ordinal, tokens, missing=0):
    value = fact(owner, ordinal, tokens, missing)
    value["payload"]["data"]["attempt_id"] = f"{owner}-{ordinal}"
    return value


def project(*records):
    turns, _ = project_history(
        [event("turn_started"), *records, event("turn_complete")], "queried-thread"
    )
    return turns[0]


def test_zero_failed_attempt_and_success_keep_detail_with_duplicate_facts():
    failed, success = attempt("owner", 1, 0, 1), attempt("owner", 2, 7, 1)
    turn = project(failed, success, success)
    first, second = turn["request_usage_entries"]
    assert turn["request_usage_entries_complete"] is True
    assert first["attempt_id"] == "owner-1" and second["attempt_id"] == "owner-2"
    assert first["request_ordinal"] == 1 and second["request_ordinal"] == 2
    assert first["token_usage_known"] is False and second["token_usage_known"] is True
    assert first["total_tokens"] == first["input_tokens"] == first["output_tokens"] == 0
    assert first["input_tokens_details"] == {"cached_tokens": 0, "cache_write_tokens": 0}
    assert first["output_tokens_details"] == {"reasoning_tokens": 0}
    assert second["input_tokens"] == second["total_tokens"] == 7
    assert first["thread_id"] == "original-thread" and first["turn_id"] == "turn"
    assert first["scope"] == second["scope"] == "sdk_model_attempt"


def test_missing_intermediate_detail_is_not_inferred_from_an_aggregate():
    turn = project(attempt("owner", 2, 7, 1))
    assert turn["run_usage"]["requests"] == 2
    assert turn["request_usage_entries"] == []
    assert turn["request_usage_entries_complete"] is False


def test_terminal_only_history_keeps_aggregate_without_inventing_detail():
    value = attempt("owner", 2, 7, 1)["payload"]["data"]
    turn = project(event("task_complete", model_attempt=value))
    assert turn["run_usage"]["requests"] == 2
    assert turn["request_usage_entries"] == []
    assert turn["request_usage_entries_complete"] is False


def test_owner_restore_and_known_zero_remain_separate_from_legacy_reports():
    turn = project(raw(5), attempt("old-owner", 1, 0), attempt("new-owner", 1, 7), raw(3))
    first, second = turn["request_usage_entries"]
    assert first["owner_attempt_id"] == "old-owner"
    assert second["owner_attempt_id"] == "new-owner"
    assert first["token_usage_known"] is True and first["total_tokens"] == 0
    assert turn["request_usage_entries_complete"] is False
    assert turn["run_usage"]["requests"] == 4
    assert len(turn["request_usage_entries"]) == 2


def test_rollback_drops_old_owner_detail_before_same_turn_id_reuse():
    turns, _ = project_history(
        [
            event("turn_started"),
            attempt("old-owner", 1, 0, 1),
            event("turn_complete"),
            event("thread_rolled_back", num_turns=1),
            event("turn_started"),
            attempt("fresh-owner", 1, 3),
            event("turn_complete"),
        ],
        "session",
    )
    assert len(turns) == 1
    assert [entry["owner_attempt_id"] for entry in turns[0]["request_usage_entries"]] == [
        "fresh-owner"
    ]
    assert turns[0]["request_usage_entries_complete"] is True


def test_measured_failure_keeps_exact_per_attempt_tokens():
    turn = project(attempt("owner", 1, 3), attempt("owner", 2, 10))
    assert [entry["input_tokens"] for entry in turn["request_usage_entries"]] == [3, 7]
    assert all(entry["token_usage_known"] for entry in turn["request_usage_entries"])
    assert turn["request_usage_entries_complete"] is True


def test_contradictory_attempt_identity_and_unknown_measurement_have_no_detail():
    reused = attempt("owner", 2, 7)
    reused["payload"]["data"]["attempt_id"] = "owner-1"
    for records in ((attempt("owner", 1, 0), reused), (attempt("owner", 1, 7, 1),)):
        turn = project(*records)
        assert turn["request_usage_entries"] == []
        assert turn["request_usage_entries_complete"] is False


def test_decreasing_cumulative_tokens_do_not_create_negative_request_usage():
    turn = project(attempt("owner", 1, 7), attempt("owner", 2, 3))
    assert turn["request_usage_entries"] == []
    assert turn["request_usage_entries_complete"] is False


def test_native_hosted_replay_updates_original_request_with_later_terminal_usage():
    first = attempt("hosted", 1, 0, 1)
    second = attempt("hosted", 2, 7, 1)
    started, settled = first["payload"]["data"], second["payload"]["data"]
    for detail in started, settled:
        detail["provider_managed"] = True
    pending = {"request_id": "physical-first", "request_ordinal": 1, "token_usage": None}
    started["provider_requests"] = [pending]
    settled["provider_requests"] = [
        {**pending, "token_usage": settled["usage"]["token_usage"]},
        {"request_id": "physical-second", "request_ordinal": 2, "token_usage": None},
    ]
    result = project(first, second, first, second, event("task_complete", model_attempt=settled))
    assert result["request_usage_entries_complete"] is True
    assert [
        (entry["attempt_id"], entry["token_usage_known"], entry["total_tokens"])
        for entry in result["request_usage_entries"]
    ] == [("physical-first", True, 7), ("physical-second", False, 0)]
