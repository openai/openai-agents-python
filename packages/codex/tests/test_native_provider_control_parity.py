"""Shared public controls reach direct and borrowed Responses providers."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, NativeProvider, ResponsesModel, function_tool
from tests._asyncio import timeout


@pytest.mark.parametrize("borrowed", [False, True], ids=["native", "openai-responses"])
async def test_responses_controls_inherit_restore_and_clear_on_the_wire(borrowed):
    requests = []

    async def respond(request):
        body = await request.json()
        requests.append(body)
        response = {
            "id": f"resp_parity_{len(requests)}",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": f"msg_parity_{len(requests)}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
                }
            ],
        }
        if not body.get("stream"):
            return web.json_response(response)
        frames = [
            {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
            {"type": "response.completed", "response": response},
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            content_type="text/event-stream",
        )

    @function_tool
    def unused() -> str:
        raise AssertionError("The scripted response has no tool call")

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with timeout(30), TestServer(app) as server:
        url = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=url, max_retries=0) as client:
            backend = (
                ResponsesModel(client, model="fixture-adapter-model", prompt_cache_key="")
                if borrowed
                else None
            )
            async with NativeProvider(
                base_url=url,
                api_key="synthetic",
                supports_websockets=False,
                model_backend=backend,
            ) as provider:
                agent = Agent(
                    model="fixture-initial-model",
                    instructions="Answer briefly.",
                    provider=provider,
                    tools=[unused],
                )
                async with agent.session() as session:
                    assert (
                        await (await session.submit("Initial")).get_result()
                    ).final_output == "Done."
                    await session.update(
                        model="fixture-updated-model",
                        model_settings={"tool_choice": "none", "prompt_cache_retention": "24h"},
                    )
                    for prompt in ("Updated", "Inherited"):
                        assert (
                            await (await session.submit(prompt)).get_result()
                        ).final_output == "Done."
                    saved = await session.save_state()
                async with agent.session(state=saved) as restored:
                    assert (
                        await (await restored.submit("Restored")).get_result()
                    ).final_output == "Done."
                    await restored.update(
                        model_settings={"tool_choice": None, "prompt_cache_retention": None}
                    )
                    assert (
                        await (await restored.submit("Cleared")).get_result()
                    ).final_output == "Done."

    assert len(requests) == 5
    assert [request["model"] for request in requests] == (
        ["fixture-adapter-model"] * 5
        if borrowed
        else ["fixture-initial-model", *["fixture-updated-model"] * 4]
    )
    assert "prompt_cache_retention" not in requests[0]
    assert [request["tool_choice"] for request in requests[1:4]] == ["none"] * 3
    assert [request["prompt_cache_retention"] for request in requests[1:4]] == ["24h"] * 3
    assert requests[-1].get("tool_choice", "auto") == "auto"
    assert "prompt_cache_retention" not in requests[-1]
    keys = [request["prompt_cache_key"] for request in requests]
    if borrowed:
        assert keys == [""] * 5
    else:
        for session_keys in (keys[:3], keys[3:]):
            assert isinstance(session_keys[0], str) and session_keys[0]
            assert session_keys == [session_keys[0]] * len(session_keys)
