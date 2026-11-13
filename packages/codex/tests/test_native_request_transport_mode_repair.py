from __future__ import annotations

import json

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, Field

from agents import Agent, NativeError, NativeProvider, OutputValidationError, PromptedOutput
from tests._asyncio import timeout


class Report(BaseModel):
    label: str = Field(min_length=2)


@pytest.mark.parametrize("source", ["agent", "updated"])
@pytest.mark.parametrize("selected", ["http", "websocket"])
async def test_prompted_repair_keeps_selected_transport(source, selected):
    wire = {"http": 0, "upgrades": 0, "generations": 0}

    def events():
        wire["generations"] += 1
        ordinal = wire["generations"]
        item = {
            "id": f"msg_{ordinal}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps({"label": "x" if ordinal == 1 else "fixed"}),
                    "annotations": [],
                }
            ],
        }
        response = {
            "id": f"resp_{ordinal}",
            "model": "gpt-5.5",
            "status": "completed",
            "output": [item],
        }
        return [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]

    async def http(request):
        await request.read()
        wire["http"] += 1
        return web.Response(
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events()),
            content_type="text/event-stream",
        )

    async def websocket(request):
        wire["upgrades"] += 1
        if selected == "http" or wire["upgrades"] > 1:
            return web.Response(status=426)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for incoming in socket:
            if incoming.type != WSMsgType.TEXT:
                continue
            frame = json.loads(incoming.data)
            if frame.get("generate") is False:
                await socket.send_json(
                    {"type": "response.completed", "response": {"id": "warmup", "output": []}}
                )
            else:
                for event in events():
                    await socket.send_json(event)
        return socket

    app = web.Application()
    app.router.add_post("/v1/responses", http)
    app.router.add_get("/v1/responses", websocket)
    async with timeout(20), TestServer(app, shutdown_timeout=0.1) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-repair-mode",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            initial = (
                selected if source == "agent" else "websocket" if selected == "http" else "http"
            )
            agent = Agent(
                instructions="Return JSON.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"request_transport": initial},
                output_type=PromptedOutput(Report, 1),
            )
            async with agent.session() as session:
                if source == "updated":
                    await session.update(model_settings={"request_transport": selected})
                turn = await session.submit("Return the label.")
                if selected == "http":
                    assert (await turn.get_result()).final_output == Report(label="fixed")
                    assert wire == {"http": 2, "upgrades": 0, "generations": 2}
                else:
                    with pytest.raises((NativeError, OutputValidationError)):
                        await turn.get_result()
                    assert wire == {"http": 0, "upgrades": 2, "generations": 1}
