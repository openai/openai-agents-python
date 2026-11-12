import json

import pytest
from pydantic import ValidationError

from agents.state import SessionState


def test_checkpoint_without_roles_preserves_native_tree_envelope():
    payload = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [{"type": "message", "content": "remember this"}],
    }
    checkpoint = SessionState.from_json(json.dumps(payload))
    assert checkpoint.subagents == {}
    assert json.loads(checkpoint.to_json()) == payload


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 1},
        {"format_version": 3},
        {"root_metadata": None},
        {"unexpected": True},
        {"history": "invalid"},
    ],
)
def test_checkpoint_rejects_incompatible_envelopes(change):
    checkpoint = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [],
        **change,
    }
    with pytest.raises(ValidationError):
        SessionState.from_json(json.dumps(checkpoint))


@pytest.mark.parametrize(
    "carrier",
    ["root", "active_child", "closed_child", "settings", "compacted", "original", "manual"],
)
def test_checkpoint_rejects_only_authoritative_saved_prompt_references(carrier):
    prompt = {"reference": {"id": "pmpt_injected"}, "omit_model": False, "omit_tools": True}
    model = {"request_controls": {"hosted_prompt": prompt}}
    metadata = {"sdk_agent_profile": {"model": model}}
    row = {"type": "response_item", "payload": {}, "metadata": metadata}
    payload = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [],
    }
    if carrier == "root":
        payload["history"] = [row]
    elif carrier.endswith("child"):
        payload["descendants"] = [
            {
                "thread_id": "child",
                "parent_thread_id": "session",
                "status": carrier.removesuffix("_child"),
                "metadata": {},
                "history": [row],
            }
        ]
    elif carrier == "settings":
        payload["history"] = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": model,
                },
            }
        ]
    elif carrier == "compacted":
        payload["history"] = [
            {
                "type": "compacted",
                "payload": {
                    "replacement_history_metadata": [metadata],
                },
            }
        ]
    elif carrier == "original":
        payload["history"] = [
            {
                "type": "compacted",
                "payload": {
                    "sdk_originals": {"items": [{"original": "opaque", "projection": row}]},
                },
            }
        ]
    else:
        payload["format_version"] = 3
        payload["native_continuation"] = {
            "turn_id": "turn",
            "seen_call_ids": [],
            "reply_order": [],
            "pending_manual": [],
            "turn_usage": dict.fromkeys(
                [
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ],
                0,
            ),
            "accepted_replies": {"call": row},
        }
    with pytest.raises(ValidationError, match="Saved prompt references"):
        SessionState.from_json(json.dumps(payload))
    prompt["reference"] = None
    assert json.loads(SessionState.from_json(json.dumps(payload)).to_json()) == payload


def test_checkpoint_preserves_prompt_shaped_user_data():
    data = {
        "metadata": {
            "sdk_agent_profile": {
                "model": {
                    "request_controls": {"hosted_prompt": {"reference": {"id": "user-data"}}},
                }
            }
        }
    }
    payload = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [
            {"type": "response_item", "payload": {"type": "function_call_output", "output": data}},
            {
                "type": "compacted",
                "payload": {
                    "sdk_originals": {
                        "items": [
                            {
                                "original": json.dumps({"projection": data}),
                                "projection": None,
                            }
                        ]
                    }
                },
            },
        ],
    }
    assert json.loads(SessionState.from_json(json.dumps(payload)).to_json()) == payload
