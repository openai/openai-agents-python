from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from _agents_codex import _hosted_responses_websocket as hosted


@pytest.mark.parametrize("terminal_only", [False, True])
@pytest.mark.parametrize("store", [False, True])
@pytest.mark.parametrize("with_users", [False, True])
async def test_terminal_before_rejection_admits_queued_calls(store, with_users, terminal_only):
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
        if not terminal_only:
            await socket.send(json.dumps({"type": "response.output_item.done", "item": calls[1]}))
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
        await socket.send(
            json.dumps(
                {"type": "response.inject.failed", "error": {"code": "response_already_completed"}}
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
        "response.create",
    ]
    assert captured[1]["input"] == first
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


@pytest.mark.parametrize("store", [False, True])
@pytest.mark.parametrize("terminal_only", [False, True])
async def test_native_terminal_before_rejection_executes_each_callback_once(store, terminal_only):
    from openai import AsyncOpenAI

    from agents import (
        Agent,
        HostedProgrammaticToolCalling,
        HostedResponsesWebSocketModel,
        NativeProvider,
        function_tool,
    )

    captured = []
    effects = []
    calls = [
        {
            "type": "function_call",
            "id": f"item_{index}",
            "call_id": f"call_{index}",
            "name": "lookup",
            "arguments": json.dumps({"n": index}),
        }
        for index in (1, 2)
    ]

    async def peer(socket):
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "response.created", "response": {"id": "response"}}))
        await socket.send(json.dumps({"type": "response.output_item.done", "item": calls[0]}))
        captured.append(json.loads(await socket.recv()))
        if not terminal_only:
            await socket.send(json.dumps({"type": "response.output_item.done", "item": calls[1]}))
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
        await socket.send(
            json.dumps(
                {"type": "response.inject.failed", "error": {"code": "response_already_completed"}}
            )
        )
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "response.created", "response": {"id": "continued"}}))
        await socket.send(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "continued",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": "answer",
                                "role": "assistant",
                                "status": "completed",
                                "phase": "final_answer",
                                "agent": {"agent_name": "/root"},
                                "content": [{"type": "output_text", "text": "complete"}],
                            }
                        ],
                    },
                }
            )
        )

    @function_tool(allowed_callers=["direct", "programmatic"])
    def lookup(n: int) -> str:
        effects.append(n)
        return f"result_{n}"

    async with asyncio.timeout(15), serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with AsyncOpenAI(
            api_key="synthetic", base_url=f"http://127.0.0.1:{port}/v1"
        ) as client:
            async with NativeProvider(
                model_backend=HostedResponsesWebSocketModel(client)
            ) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Use both lookups.",
                    provider=provider,
                    model_settings={"store": store},
                    tools=[lookup, HostedProgrammaticToolCalling()],
                )
                result = await agent.run("Look up both values.")
                assert result.final_output == "complete"
    assert effects == [1, 2]
    assert [frame["type"] for frame in captured] == [
        "response.create",
        "response.inject",
        "response.create",
    ]
    outputs = [item for item in captured[-1]["input"] if item["type"] == "function_call_output"]
    assert [(item["call_id"], item["output"]) for item in outputs] == [
        ("call_1", "result_1"),
        ("call_2", "result_2"),
    ]
