from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex import _hosted_responses_websocket as hosted, _responses_websocket as transport


def test_cumulative_budget_precedes_second_json_materialization(monkeypatch):
    budget = transport._SocketJsonBudget()
    frame = json.dumps({"dense": [{}] * 40_000})
    calls = 0
    original = json.loads

    def decode(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(transport.json, "loads", decode)
    transport._decode_socket_frame(frame, None, budget=budget)
    remaining = budget.remaining
    with pytest.raises(ValueError, match="invalid JSON frame"):
        transport._decode_socket_frame(frame, None, budget=budget)
    assert calls == 1
    assert 0 < remaining == budget.remaining < transport._MAX_SOCKET_JSON_NODES


async def test_reconciliation_bounds_retained_frame_overhead():
    class Socket:
        reads = 0

        async def recv(self):
            self.reads += 1
            return '{"type":"tick"}'

    socket = Socket()
    run = hosted._Run(request=cast(Any, SimpleNamespace(_observe_raw_provider_events=True)))
    active = hosted._Active(create={}, lineage="synthetic-lineage")
    wrapper = hosted._Socket(socket, run, {}, None)
    with pytest.raises(ValueError, match="retained frame limit"):
        await wrapper._reconcile_terminal(active)
    assert run.retired
    assert len(run.recovered) == socket.reads == hosted._MAX_RESPONSE_ITEMS
    assert 0 <= run.recovered_budget.remaining < transport._MAX_SOCKET_JSON_NODES
