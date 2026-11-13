from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    HostedAgentMetadata,
    HostedResponsesWebSocketModel,
    NativeProvider,
    ToolContext,
    function_tool,
    get_hosted_agent_metadata,
)
from tests._asyncio import timeout


async def test_attribution_stays_with_selected_handoff_owner_across_reused_provider_ids() -> None:
    frames: list[dict[str, Any]] = []
    effects: list[str] = []
    seen: list[HostedAgentMetadata | None] = []
    runs = 0
    final = {
        "type": "message",
        "id": "msg_root",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "agent": {"agent_name": "/root"},
        "content": [{"type": "output_text", "text": "done"}],
    }

    async def route(request: web.Request) -> web.WebSocketResponse:
        nonlocal runs
        run = runs
        runs += 1
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        injections = 0
        handoff = {
            "type": "function_call",
            "id": "fc_handoff",
            "call_id": "call_handoff",
            "name": "transfer_to_b",
            "arguments": "{}",
            "agent": {"agent_name": "/remote-owner"},
            "phase": "analysis",
        }
        lookup = {
            "type": "function_call",
            "id": "fc_reused",
            "call_id": "call_reused",
            "name": "lookup",
            "arguments": "{}",
            **(
                {
                    "agent": {"agent_name": "/worker-one" if run == 0 else "/worker-two"},
                    "phase": "analysis" if run == 0 else "commentary",
                }
                if run < 2
                else {}
            ),
        }
        async for message in socket:
            frame = json.loads(message.data)
            frames.append(frame)
            if frame["type"] == "response.create":
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_reused", "output": []}}
                )
                await socket.send_json(
                    {"type": "response.output_item.done", "output_index": 0, "item": handoff}
                )
            else:
                assert frame["type"] == "response.inject"
                injections += 1
                await socket.send_json(
                    {"type": "response.inject.created", "response_id": "resp_reused"}
                )
                if injections == 1:
                    await socket.send_json(
                        {"type": "response.output_item.done", "output_index": 1, "item": lookup}
                    )
                else:
                    assert injections == 2
                    hidden = {
                        **final,
                        "id": "msg_private",
                        "agent": {"agent_name": "/other"},
                        "content": [{"type": "output_text", "text": "not final user text"}],
                    }
                    await socket.send_json(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_reused",
                                "status": "completed",
                                "output": [handoff, lookup, hidden, final],
                                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                            },
                        }
                    )
        return socket

    @function_tool(name="lookup")
    def parent_lookup() -> str:
        effects.append("parent")
        return "wrong owner"

    @function_tool(name="lookup")
    def selected_lookup(context: ToolContext[Any]) -> str:
        effects.append("selected")
        seen.append(get_hosted_agent_metadata(context))
        assert context.call.tool.name == "lookup"
        assert context.call.route == "direct"
        assert context.call.child_id is None
        return "selected result"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(30), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            model = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=model) as provider:
                selected = Agent(
                    name="B", instructions="Use lookup and finish.", tools=[selected_lookup]
                )
                parent = Agent(
                    name="A",
                    model="gpt-5.5",
                    instructions="Transfer to B.",
                    provider=provider,
                    tools=[parent_lookup],
                    handoffs=[selected],
                )
                for _ in range(3):
                    assert (await parent.run("start")).final_output == "done"
            assert not client.is_closed()
    assert effects == ["selected"] * 3
    assert seen == [
        HostedAgentMetadata("/worker-one", "analysis"),
        HostedAgentMetadata("/worker-two", "commentary"),
        None,
    ]
    assert [frame["type"] for frame in frames] == [
        "response.create",
        "response.inject",
        "response.inject",
    ] * 3
