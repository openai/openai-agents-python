"""Typed received facts preserve privacy, selection and native replay boundaries."""

from __future__ import annotations

import json

import pytest

from _agents_codex.agent import _copy_model_settings
from _agents_codex.observation import EventStream, ResponseFactReceived, _ObservationBuffer
from _agents_codex.response_facts import _response_fact_record


def record(kind: str = "refusal") -> dict:
    fact = {
        "kind": "refusal",
        "item_id": "item",
        "content_index": 0,
        "snapshot": True,
        "text": "SYNTHETIC_PRIVATE_REFUSAL",
        "capture_complete": True,
    }
    if kind == "text_probabilities":
        fact = {
            "kind": kind,
            "item_id": "item",
            "output_index": 0,
            "content_index": 0,
            "snapshot": False,
            "delta": "a",
            "capture_complete": True,
            "tokens": [
                {
                    "token": "a",
                    "bytes": [97],
                    "logprob": -1,
                    "top_logprobs": [
                        {"token": "SYNTHETIC_PRIVATE_ALTERNATIVE", "bytes": [], "logprob": -2}
                    ],
                }
            ],
        }
    return {
        "id": "fact",
        "thread_id": "thread",
        "turn_id": "turn",
        "request_id": "native-request",
        "response_id": "response",
        "fact": fact,
    }


@pytest.mark.parametrize("value", [0, 20, None])
def test_probability_selection_preserves_zero_and_clear(value: int | None) -> None:
    assert _copy_model_settings({"top_logprobs": value}) == {"top_logprobs": value}


@pytest.mark.parametrize("value", [-1, 256, 1.5, True, "1"])
def test_probability_selection_rejects_invalid_local_shapes(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="top_logprobs"):
        _copy_model_settings({"top_logprobs": value})


def test_fact_projection_selects_fields_and_preserves_missing_vs_empty() -> None:
    raw = record("text_probabilities")
    raw["authorization"] = "NEVER_FORWARD"
    raw["fact"]["tokens"][0]["authorization"] = "NEVER_FORWARD"
    projected = _response_fact_record(raw)
    assert projected is not None
    assert "NEVER_FORWARD" not in json.dumps(projected)
    assert projected["request_id"] != projected["response_id"]
    token = projected["fact"]["tokens"][0]
    assert token["bytes"] == [97]
    assert token["top_logprobs"][0]["bytes"] == []
    del raw["fact"]["tokens"][0]["bytes"]
    del raw["fact"]["tokens"][0]["top_logprobs"]
    missing = _response_fact_record(raw)
    assert missing is not None
    assert "bytes" not in missing["fact"]["tokens"][0]
    assert "top_logprobs" not in missing["fact"]["tokens"][0]


@pytest.mark.parametrize("kind", ["refusal", "text_probabilities"])
async def test_default_stream_is_content_free_and_trusted_readers_are_independent(
    kind: str,
) -> None:
    buffer = _ObservationBuffer("session")
    item = _response_fact_record(record(kind))
    assert item is not None
    event = ResponseFactReceived(
        session_id="session", turn_id="turn", submission_id="submission", record=item
    )
    buffer.append(event)
    ordinary = EventStream(buffer, after=0)
    trusted = EventStream(buffer, after=0, _include_execution_details=True)
    a, b = await anext(ordinary), await anext(trusted)
    assert "SYNTHETIC_PRIVATE" not in repr(a)
    assert "SYNTHETIC_PRIVATE" not in repr(b)
    assert "SYNTHETIC_PRIVATE" not in json.dumps(a.record)
    assert "SYNTHETIC_PRIVATE" in json.dumps(b.record)
    b.record["fact"].clear()
    another = EventStream(buffer, after=0, _include_execution_details=True)
    c = await anext(another)
    assert "SYNTHETIC_PRIVATE" in json.dumps(c.record)
    await ordinary.aclose()
    await trusted.aclose()
    await another.aclose()


async def test_large_fact_uses_existing_observation_gap_budget() -> None:
    buffer = _ObservationBuffer("session")
    raw = record()
    raw["fact"]["text"] = "p" * 70_000
    projected = _response_fact_record(raw)
    assert projected is not None
    buffer.append(
        ResponseFactReceived(
            session_id="session", turn_id="turn", submission_id="submission", record=projected
        )
    )
    stream = EventStream(buffer, after=0)
    gap = await anext(stream)
    assert gap.type == "gap"
    assert not buffer.records
    await stream.aclose()
