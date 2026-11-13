"""A compatible Chat Completions provider receives the exact ordered video part."""

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from _agents_codex._media import _capture_video
from agents import (
    Agent,
    ChatCompletionsModel,
    NativeProvider,
    ResponsesModel,
    SessionState,
    function_tool,
)
from agents.input import UserMessage
from tests._asyncio import timeout

URL = "https://media.example.invalid/example.mp4?version=1"
INPUT: UserMessage = {
    "role": "user",
    "content": [
        {"type": "input_text", "text": "before"},
        {"type": "input_video", "video_url": URL},
        {"type": "input_text", "text": "after"},
    ],
}
WIRE = [
    {"type": "text", "text": "before"},
    {"type": "video_url", "video_url": {"url": URL}},
    {"type": "text", "text": "after"},
]


@pytest.mark.parametrize(
    "url",
    [
        "https://synthetic-private-video\uff0fsecret.example/video.mp4",
        "https://synthetic-private-video.example/\ud800",
    ],
)
def test_invalid_video_url_diagnostics_do_not_retain_the_reference(url: str) -> None:
    with pytest.raises(ValueError, match="^Video URL ") as error:
        _capture_video({"video_url": url})
    assert "synthetic-private-video" not in str(error.value)
    assert error.value.__context__ is None


@pytest.mark.parametrize("stream", [False, True])
async def test_native_video_keeps_its_position_across_turns_and_restore(stream: bool) -> None:
    requests: list[dict[str, Any]] = []
    effects: list[str] = []

    @function_tool
    def inspect_reference() -> str:
        effects.append("inspected")
        return "reference inspected"

    async def chat(request: web.Request) -> web.Response:
        body = await request.json()
        requests.append(body)
        common = {"id": "chatcmpl_video", "created": 1, "model": "video-model"}
        first_call = len(requests) == 1
        tool_call = {
            "id": "call_inspect_reference",
            "type": "function",
            "function": {"name": "inspect_reference", "arguments": "{}"},
        }
        finish = "tool_calls" if first_call else "stop"
        content = {"tool_calls": [tool_call]} if first_call else {"content": "accepted"}
        if stream:
            if first_call:
                content = {"tool_calls": [{"index": 0, **tool_call}]}
            chunks = [
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", **content},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                },
            ]
            return web.Response(
                text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                + "data: [DONE]\n\n",
                content_type="text/event-stream",
            )
        return web.json_response(
            {
                **common,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", **content},
                        "finish_reason": finish,
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    async with timeout(30), TestServer(app) as server:
        endpoint = str(server.make_url("/v1"))
        async with (
            AsyncOpenAI(base_url=endpoint, api_key="synthetic-video", max_retries=0) as client,
            NativeProvider(
                model_backend=ChatCompletionsModel(client, "video-model", stream=stream)
            ) as provider,
        ):
            agent = Agent(
                model="video-model",
                instructions="Inspect.",
                provider=provider,
                tools=[inspect_reference],
            )
            async with agent.session() as session:
                first = await session.submit(INPUT)
                assert (await first.get_result()).final_output == "accepted"
                second = await session.submit("keep the context")
                assert (await second.get_result()).final_output == "accepted"
                state = await session.save_state()
            async with agent.session(state=SessionState.from_json(state.to_json())) as restored:
                third = await restored.submit("keep the restored context")
                assert (await third.get_result()).final_output == "accepted"
    assert len(requests) == 4
    assert effects == ["inspected"]
    for body in requests:
        assert body["stream"] is stream
        assert next(item["content"] for item in body["messages"] if item["role"] == "user") == WIRE


async def test_borrowed_responses_rejects_video_without_sending_it() -> None:
    requests = []

    async def responses(request: web.Request) -> web.Response:
        requests.append(await request.json())
        return web.json_response({"error": "unexpected request"}, status=400)

    app = web.Application()
    app.router.add_post("/v1/responses", responses)
    async with timeout(30), TestServer(app) as server:
        async with (
            AsyncOpenAI(
                base_url=str(server.make_url("/v1")), api_key="synthetic-video", max_retries=0
            ) as client,
            NativeProvider(model_backend=ResponsesModel(client, "video-model")) as provider,
        ):
            agent = Agent(model="video-model", instructions="Inspect.", provider=provider)
            with pytest.raises(ValueError, match="Video input requires"):
                await agent.run(INPUT)
    assert requests == []
