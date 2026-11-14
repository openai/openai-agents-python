import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    AnyLLMModel,
    LitellmModel,
    NativeProvider,
    SessionState,
    ToolOutput,
    ToolOutputFile,
    ToolOutputImage,
    ToolOutputText,
    function_tool,
)

IMAGE = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQV"
    "R4nGPkUbJgwAaYsIoOWgkAVcAAdmJlY/oAAAAASUVORK5CYII="
)
FILE_DATA = "data:text/plain;base64,YXNzZXQ="

TOOL = {
    "id": "call_inspect",
    "type": "function",
    "function": {"name": "inspect_asset", "arguments": "{}"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("backend", ["litellm", "anyllm"])
@pytest.mark.parametrize("media", ["image", "file", "file-id"])
async def test_borrowed_structured_tool_output_reaches_next_request(
    monkeypatch, stream, backend, media
):
    wire = []
    effects = []

    async def route(request):
        body = await request.json()
        wire.append(body)
        first = len(wire) == 1
        message = {"role": "assistant", "content": None if first else "Complete."}
        if first:
            message["tool_calls"] = [TOOL]
        finish = "tool_calls" if first else "stop"
        shared = {"id": f"chat_{len(wire)}", "model": "gpt-4o-mini", "created": 1}
        if not body.get("stream"):
            return web.json_response(
                {
                    **shared,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                }
            )
        if first:
            message["tool_calls"] = [{"index": 0, **TOOL}]
        frames = [
            {
                **shared,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
            for delta, reason in ((message, None), ({}, finish))
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", route)
    async with TestServer(app) as server:
        base = str(server.make_url("/v1"))

        @function_tool
        def inspect_asset() -> ToolOutput:
            effects.append("once")
            part = {
                "image": ToolOutputImage(IMAGE, detail="high"),
                "file": ToolOutputFile(file_data=FILE_DATA, filename="asset.txt"),
                "file-id": ToolOutputFile(file_id="file-fixture-owned"),
            }[media]
            return ToolOutput([ToolOutputText("asset label"), part])

        async def run(adapter):
            async with NativeProvider(model_backend=adapter) as provider:
                agent = Agent(
                    name="asset",
                    model="gpt-4o-mini",
                    instructions="Inspect the asset.",
                    tools=[inspect_asset],
                    provider=provider,
                )
                async with agent.session() as session:
                    initial = await (await session.submit("Use the tool.")).get_result()
                    assert initial.final_output == "Complete."
                    saved = await session.save_state()
                async with agent.session(state=SessionState.from_json(saved.to_json())) as session:
                    return await (await session.submit("Continue.")).get_result()

        if backend == "anyllm":
            any_llm = pytest.importorskip("any_llm")
            client = any_llm.AnyLLM.create(
                "openai", api_base=base, api_key="synthetic-tool-output", max_retries=0
            )
            try:
                result = await run(AnyLLMModel(client, "gpt-4o-mini", stream=stream))
            finally:
                await client.client.close()
        else:
            monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            litellm = pytest.importorskip("litellm")
            monkeypatch.setattr(litellm, "telemetry", False)
            async with AsyncOpenAI(
                base_url=base, api_key="synthetic-tool-output", max_retries=0
            ) as client:

                async def create(**options):
                    options.setdefault("num_retries", 0)
                    return await litellm.acompletion(
                        **options,
                        client=client,
                        api_base=base,
                        api_key="synthetic-tool-output",
                        allowed_openai_params=["tool_choice"],
                    )

                result = await run(
                    LitellmModel(
                        SimpleNamespace(acompletion=create), "openai/gpt-4o-mini", stream=stream
                    )
                )
        assert result.final_output == "Complete."
    assert effects == ["once"]
    assert len(wire) == 3
    assert [bool(request.get("stream")) for request in wire] == [stream, stream, stream]
    projected_media = {
        "image": {"type": "image_url", "image_url": {"url": IMAGE, "detail": "high"}},
        "file": {"type": "file", "file": {"file_data": FILE_DATA, "filename": "asset.txt"}},
        "file-id": {"type": "file", "file": {"file_id": "file-fixture-owned"}},
    }[media]
    expected = [{"type": "text", "text": "asset label"}, projected_media]
    for request in wire[1:]:
        [output] = [message for message in request["messages"] if message["role"] == "tool"]
        assert output["tool_call_id"] == TOOL["id"]
        assert output["content"] == expected
