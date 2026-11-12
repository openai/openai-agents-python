"""Content-free native attempt projections have been retaining exact counts."""

import asyncio
import json
from dataclasses import FrozenInstanceError, asdict
from types import SimpleNamespace

import pytest

import agents
from agents import RequestAttemptCount, RequestAttempts
from agents.turn import _root_turn


class Capture:
    def __init__(self, value=None):
        self.value = value

    def _snapshot(self):
        return None if self.value is None else json.dumps(self.value)


async def test_snapshot_waits_for_settlement_and_survives_owner_close():
    session = SimpleNamespace(id="fixture-session", _owner=object())
    turn = _root_turn(session, "fixture-turn", "fixture-submission", asyncio.Future())
    assert turn.request_attempts is None
    capture = Capture()
    object.__setattr__(turn, "_request_attempts", capture)
    assert turn.request_attempts is None
    capture.value = {
        "availability": "partial",
        "counts": [
            {
                "owner": "root",
                "operation": "generation",
                "transport": "http_sse",
                "count": str(2**64 - 1),
            }
        ],
        "borrowed_requests_unavailable": True,
        "private_payload": "synthetic-private-content",
    }
    snapshot = turn.request_attempts
    assert snapshot == RequestAttempts(
        "partial", (RequestAttemptCount("root", "generation", "http_sse", 2**64 - 1),), True
    )
    assert agents.RequestAttempts is RequestAttempts
    assert "synthetic-private-content" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.availability = "complete"
    capture.value["counts"].clear()
    assert len(snapshot.counts) == 1
    assert turn.request_attempts.counts == ()


@pytest.mark.parametrize("borrowed", [False, True])
def test_empty_settled_report_keeps_availability_distinct(borrowed):
    from _agents_codex.turn import _request_attempts_snapshot

    value = {
        "availability": "unavailable" if borrowed else "complete",
        "counts": [],
        "borrowed_requests_unavailable": borrowed,
    }
    snapshot = _request_attempts_snapshot(Capture(value))
    assert asdict(snapshot) == {**value, "counts": ()}


@pytest.mark.parametrize("count", [1, True, "", "-1", "1.5", "١"])
def test_invalid_wire_count_never_becomes_a_public_number(count):
    from _agents_codex.turn import _request_attempts_snapshot

    capture = Capture(
        {
            "availability": "complete",
            "borrowed_requests_unavailable": False,
            "counts": [
                {
                    "owner": "root",
                    "operation": "generation",
                    "transport": "http_sse",
                    "count": count,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="request-attempt"):
        _request_attempts_snapshot(capture)
