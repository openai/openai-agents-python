import json

import pytest
from pydantic import ValidationError

from agents import SessionState


def saved(**extra):
    return {
        "format_version": 3,
        "codex_revision": "revision",
        "source_session_id": "root",
        "history": [],
        "root_metadata": {},
        "native_continuation": {
            "turn_id": "turn",
            "seen_call_ids": [],
            "turn_usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            },
            "reply_order": ["pending"],
            "accepted_replies": {},
            "pending_manual": [{"call_id": "pending", "tool": "lookup", "arguments": "{}"}],
        },
        **extra,
    }


@pytest.mark.parametrize(
    "input",
    [
        "",
        "exact original string",
        {
            "role": "user",
            "type": "message",
            "content": [{"type": "input_text", "text": "exact original message"}],
        },
    ],
)
def test_original_input_round_trip(input):
    state = SessionState.from_json(json.dumps(saved(original_input=input)))
    assert state.original_input == input
    assert json.loads(state.to_json())["original_input"] == input


@pytest.mark.parametrize(
    "input", [5, {"role": "assistant", "content": "invalid"}, {"role": "user", "content": []}]
)
def test_invalid_original_input_is_rejected(input):
    with pytest.raises((ValidationError, TypeError)):
        SessionState.from_json(json.dumps(saved(original_input=input)))


def test_pre_change_state_has_no_invented_input():
    state = SessionState.from_json(json.dumps(saved()))
    assert state.original_input is None
    assert "original_input" not in json.loads(state.to_json())


def test_idle_state_cannot_carry_another_run_origin():
    state = saved(original_input="another run")
    state["format_version"] = 2
    del state["native_continuation"]
    with pytest.raises(ValidationError, match="requires a manual continuation"):
        SessionState.from_json(json.dumps(state))


@pytest.mark.parametrize("version", [8, 9])
def test_original_input_round_trip_with_request_budget(version):
    state = SessionState.from_json(
        json.dumps(
            saved(
                format_version=version,
                output_version=1,
                output_fingerprint="0" * 64,
                run_budget={"version": 1, "max_requests": 2, "admitted_requests": 1},
                original_input="original budgeted request",
            )
        )
    )
    restored = SessionState.from_json(state.to_json())
    assert restored.original_input == "original budgeted request"
    assert restored.run_budget is not None
    assert restored.run_budget.admitted_requests == 1
