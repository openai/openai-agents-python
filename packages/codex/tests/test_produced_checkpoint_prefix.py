from __future__ import annotations

import json

import pytest

from _agents_codex._produced_prefix import _MAX_BYTES, _MAX_RECORDS, _ProducedPrefix


def make_record(identity):
    return {"record": {"type": "response_item", "id": identity}, "agent": None}


def test_repeated_checkpoint_parts_keep_order_and_same_occurrences():
    earlier = make_record("same-id")
    later = {**earlier, "tool_success": False}
    parts = _ProducedPrefix().extend(json.dumps([earlier, later]))
    captured = _ProducedPrefix().extend(parts.encode()).extend(json.dumps([earlier]))
    assert captured.rows() == [earlier, later, earlier]
    assert captured.count == 3
    assert captured.bytes == len(captured.encode().encode())
    assert captured.extend("[]") is captured


def test_shared_record_and_byte_limits_cover_all_segments():
    one = json.dumps([make_record("A")] * (_MAX_RECORDS - 1))
    next_item = json.dumps([make_record("B")])
    exactly = _ProducedPrefix().extend(one).extend(next_item)
    assert exactly.count == _MAX_RECORDS
    with pytest.raises(ValueError, match="one-shot result limit"):
        exactly.extend(next_item)
    first = json.dumps([make_record("a" * (_MAX_BYTES // 2))])
    with pytest.raises(ValueError, match="one-shot result limit"):
        _ProducedPrefix().extend(first).extend(first)


@pytest.mark.parametrize("malformed", ["{}", "[1]", '[{"record":{}}]'])
def test_invalid_saved_prefix_is_refused(malformed):
    with pytest.raises(ValueError, match="Invalid native"):
        _ProducedPrefix().extend(malformed)


def test_sealed_reply_order_discards_only_pending_or_buffered_outputs():
    def record(kind, call, output=None):
        return {
            "record": {
                "type": "response_item",
                "payload": {
                    "type": kind,
                    "call_id": call,
                    "output": output,
                },
            },
            "agent": None,
        }

    earlier = record("function_call_output", "already-present", "aborted by user after 0.1s")
    settled = record("function_call_output", "settled", "aborted by user after 0.1s")
    call = record("function_call", "pending")
    replaced = record("function_call_output", "pending", "aborted by user after 0.1s")
    buffered = record("custom_tool_call_output", "queued", "already committed")
    after = record("function_call_output", "pending", "real")
    prefix = _ProducedPrefix().extend(json.dumps([earlier]))
    saved = prefix.extend(
        json.dumps([settled, call, replaced, buffered]),
        replayed_calls=frozenset({"pending", "queued", "already-present"}),
    )
    result = saved.extend(json.dumps([after]))
    assert result.rows() == [earlier, settled, call, after]
    assert result.bytes == len(result.encode().encode())
    assert result.count == 4


def test_only_replayed_outputs_produce_no_empty_segment():
    value = [
        {
            "record": {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "pending",
                },
            },
            "agent": None,
        }
    ]
    prefix = _ProducedPrefix()
    assert prefix.extend(json.dumps(value), replayed_calls=frozenset({"pending"})) is prefix


def test_prefix_limit_does_not_leave_payload_in_sdk_traceback():
    existing = _ProducedPrefix().extend(json.dumps([make_record("original")] * _MAX_RECORDS))
    extra = json.dumps([make_record("synthetic-private-value")])
    with pytest.raises(ValueError, match="one-shot result limit") as caught:
        existing.extend(extra, replayed_calls=frozenset({"unrelated-call"}))
    traceback = caught.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_name == "extend":
            assert set(frame.f_locals).isdisjoint(
                {"self", "encoded", "replayed_calls", "value", "part", "kept"}
            )
            break
        traceback = traceback.tb_next
    else:
        pytest.fail("Missing configured prefix frame")


def test_reply_replay_filter_precedes_combined_byte_limit():
    first = make_record("a" * (_MAX_BYTES // 2))
    next_call = make_record("pending-call")
    replaced = {
        "record": {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "pending-call",
                "output": "a" * (_MAX_BYTES // 2),
            },
        },
        "agent": None,
    }
    prefix = _ProducedPrefix().extend(json.dumps([first]))
    filtered = prefix.extend(
        json.dumps([next_call, replaced]), replayed_calls=frozenset({"pending-call"})
    )
    assert filtered.rows() == [first, next_call]
    assert filtered.bytes == len(filtered.encode().encode())
