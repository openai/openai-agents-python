from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from _agents_codex import _hosted_responses_websocket as hosted


@pytest.mark.parametrize("store", [False, True])
@pytest.mark.parametrize("with_users", [False, True])
async def test_rejected_injections_preserve_all_inputs_on_physical_socket(store, with_users):
    captured = []
    calls = [
        {
            "type": "function_call",
            "id": f"item_{index}",
            "call_id": f"call_{index}",
            "name": "lookup",
            "arguments": "{}",
        }
        for index in (1, 2)
    ]

    async def peer(socket):
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "response.created", "response": {"id": "response"}}))
        await socket.send(json.dumps({"type": "response.output_item.done", "item": calls[0]}))
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "response.output_item.done", "item": calls[1]}))
        failure = {
            "type": "response.inject.failed",
            "error": {"code": "response_already_completed"},
        }
        await socket.send(json.dumps(failure))
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps(failure))
        await socket.send(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "response",
                        "store": store,
                        "status": "completed",
                        "output": calls,
                    },
                }
            )
        )
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "response.created", "response": {"id": "continued"}}))

    original = {"type": "message", "role": "user", "id": "original", "content": "start"}
    results = [
        {"type": "function_call_output", "call_id": f"call_{index}", "output": f"result_{index}"}
        for index in (1, 2)
    ]
    users = [
        {"type": "message", "role": "user", "id": f"user_{index}", "content": f"followup_{index}"}
        for index in (1, 2)
    ]
    async with asyncio.timeout(5), serve(peer, "127.0.0.1", 0) as server:
        async with connect(
            f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
        ) as socket:
            run = hosted._Run(account=lambda op: "lineage" if op["operation"] == "create" else None)
            wrapper = hosted._Socket(socket, run, {}, None)
            frame = {
                "type": "response.create",
                "model": "gpt-5.5",
                "store": store,
                "input": [original],
            }
            await wrapper.send(json.dumps(frame))
            assert (await wrapper.recv())["type"] == "response.created"
            assert (await wrapper.recv())["item"]["call_id"] == "call_1"
            assert (await wrapper.recv())["type"] == "response.completed"
            first = [results[0], *([users[0]] if with_users else [])]
            await wrapper.send(json.dumps({**frame, "input": [original, *first]}))
            assert (await wrapper.recv())["item"]["call_id"] == "call_2"
            assert (await wrapper.recv())["type"] == "response.completed"
            second = [results[1], *([users[1]] if with_users else [])]
            await wrapper.send(json.dumps({**frame, "input": [original, *first, *second]}))
            assert (await wrapper.recv())["response"]["id"] == "continued"
    assert [frame["type"] for frame in captured] == [
        "response.create",
        "response.inject",
        "response.inject",
        "response.create",
    ]
    rejected = [*captured[1]["input"], *captured[2]["input"]]
    assert rejected == [*first, *second]
    replay = captured[-1]["input"]
    forwarded = [
        item
        for item in replay
        if item.get("type") == "function_call_output" or item.get("id") in {"user_1", "user_2"}
    ]
    assert forwarded == [*first, *second]
    if store:
        assert captured[-1]["previous_response_id"] == "response"
    else:
        assert "previous_response_id" not in captured[-1]
        assert [item["call_id"] for item in replay if item.get("type") == "function_call"] == [
            "call_1",
            "call_2",
        ]


@pytest.mark.parametrize("count", [20_000, 40_000])
async def test_create_limits_include_the_actual_envelope(count):
    class Socket:
        sent = []

        async def send(self, frame):
            self.sent.append(json.loads(frame))

    accounted = []

    def account(operation):
        accounted.append(operation["operation"])
        return "lineage"

    raw = Socket()
    config = {"agents": [{} for _ in range(count)]}
    frame = {"input": [{} for _ in range(count)]}
    wrapper = hosted._Socket(raw, hosted._Run(account=account), config, None)
    if count == 40_000:
        with pytest.raises(ValueError, match="node limit"):
            await wrapper._create(frame)
        assert raw.sent == []
        assert accounted == []
    else:
        await wrapper._create(frame)
        assert raw.sent == [{**frame, "type": "response.create", "multi_agent": config}]
        assert accounted == ["create", "dispatch"]
