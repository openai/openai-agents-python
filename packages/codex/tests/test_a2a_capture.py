"""Callback ownership through the public provider and real loopback transport."""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytest.importorskip("a2a")

from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel
from starlette.responses import JSONResponse

from agents.a2a import A2AEndpoint, A2AJobProvider
from agents.experimental.deferred import JobBinding, JobClaim
from tests.a2a_http import card, rpc_service, serve


class Query(BaseModel):
    text: str


@pytest.mark.parametrize("callback", ["encode", "project"])
async def test_captures_callback_values_before_async_handoff(callback, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    projected = asyncio.Event()
    first = None
    discovery_count = 0
    sent = []
    original_aclose = httpx.AsyncClient.aclose

    async def gated_aclose(client):
        # Hold transport cleanup after projection, without replacing SDK code.
        if callback == "project" and asyncio.current_task() is first and projected.is_set():
            entered.set()
            await release.wait()
        await original_aclose(client)

    if callback == "project":
        monkeypatch.setattr(httpx.AsyncClient, "aclose", gated_aclose)

    def app(url):
        async def discovery(request):
            nonlocal discovery_count
            discovery_count += 1
            if callback == "encode" and discovery_count == 1:
                entered.set()
                await release.wait()
            return JSONResponse(MessageToDict(card(url)))

        async def rpc(request):
            body = await request.json()
            text = body["params"]["message"]["parts"][0]["text"]
            sent.append(text)
            task = p.Task(
                id="task-" + text,
                context_id="context",
                status=p.TaskStatus(state=p.TASK_STATE_COMPLETED),
                artifacts=[p.Artifact(artifact_id="result", parts=[p.Part(text=text)])],
            )
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"task": MessageToDict(task)}}
            )

        return rpc_service(url, rpc, discovery=discovery)

    async def authorize(claim, operation):
        pass

    async def authorization():
        return "alice-token"

    async def reserve(claim, operation, action):
        return True

    message = p.Message()
    output = {"nested": {"text": ""}}
    labels = ("approved",)

    def encode(query):
        message.CopyFrom(
            p.Message(message_id=query.text, role=p.ROLE_USER, parts=[p.Part(text=query.text)])
        )
        return message

    def project(task):
        text = task.artifacts[0].parts[0].text
        output["nested"]["text"] = text
        if text == "A":
            projected.set()
        else:
            assert not first.done()
        return {"output": output, "labels": labels}

    async with serve(app) as url:
        provider = A2AJobProvider(
            A2AEndpoint(
                name="remote", card_url=url + "/.well-known/agent-card.json", rpc_url=url + "/rpc"
            ),
            authorize=authorize,
            authorization=authorization,
            reserve=reserve,
            encode=encode,
            project=project,
        )

        def claim(key):
            return JobClaim(
                binding=JobBinding(
                    tenant="tenant",
                    run_id="run",
                    provider_id=provider.id,
                    session_id="session",
                    turn_id="turn",
                    call_id=key,
                    tool_name="remote",
                    request_digest=key,
                    checkpoint_digest="checkpoint",
                ),
                key=key,
                generation=1,
                action="start",
            )

        first = asyncio.create_task(provider.start(claim("A"), Query(text="A")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = await provider.start(claim("B"), Query(text="B"))
            release.set()
            result = await asyncio.wait_for(first, 5)
            assert result.status == second.status == "completed"
            assert result.value == {"output": {"nested": {"text": "A"}}, "labels": ["approved"]}
            assert second.value == {"output": {"nested": {"text": "B"}}, "labels": ["approved"]}
            assert sorted(sent) == ["A", "B"]
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
