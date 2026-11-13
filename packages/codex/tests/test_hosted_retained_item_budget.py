from __future__ import annotations

import json

import pytest

from _agents_codex import _hosted_responses_websocket as hosted


@pytest.mark.parametrize("kind", ["program", "function_call"])
@pytest.mark.parametrize("count", [1, 3])
async def test_pending_injection_bounds_retained_item_nodes(kind, count):
    items = [
        {
            "type": kind,
            "id": f"buffered_{index}",
            "call_id": f"buffered_{index}",
            "code": "pass",
            "name": "lookup",
            "arguments": "{}",
            "provider_fields": {"dense": [{}] * 24_000},
        }
        for index in range(count)
    ]
    frames = [json.dumps({"type": "response.output_item.done", "item": item}) for item in items]
    frames.append(json.dumps({"type": "response.inject.created", "response_id": "response"}))

    class Socket:
        reads = 0

        async def recv(self):
            frame = frames[self.reads]
            self.reads += 1
            return frame

    raw = Socket()
    active = hosted._Active(
        create={},
        lineage="synthetic",
        response_id="response",
        template={"id": "response"},
        pending={"injected"},
        injecting={"injected"},
        injection_input=[{"type": "function_call_output", "call_id": "injected", "output": "done"}],
    )
    wrapper = hosted._Socket(raw, hosted._Run(active=active), {}, None)
    if count == 1:
        event = await wrapper.recv()
        assert event["item"] == items[0]
        assert raw.reads == 2
    else:
        with pytest.raises(ValueError, match="node limit"):
            await wrapper.recv()
        assert len(active.queued) <= 2
        assert raw.reads == 3


async def test_retained_item_allowance_spans_queue_flushes():
    class Socket:
        async def recv(self):
            return json.dumps(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "program",
                        "call_id": "program",
                        "provider_fields": {"dense": [{}] * 40_000},
                    },
                }
            )

    active = hosted._Active(
        create={}, lineage="synthetic", response_id="response", template={"id": "response"}
    )
    wrapper = hosted._Socket(Socket(), hosted._Run(active=active), {}, None)
    assert (await wrapper.recv())["type"] == "response.output_item.done"
    with pytest.raises(ValueError, match="node limit"):
        await wrapper.recv()
