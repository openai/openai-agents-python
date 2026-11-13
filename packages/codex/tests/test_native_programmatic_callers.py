from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    CodeInterpreter,
    HostedProgrammaticToolCalling,
    HostedResponsesWebSocketModel,
    NativeError,
    NativeProvider,
    function_tool,
)
from agents.observation import ModelOutputItemCompleted
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses


@pytest.mark.parametrize("stored", [False, True])
async def test_hosted_program_open_precedes_its_native_callback(stored: bool) -> None:
    requests: list[dict[str, Any]] = []
    effects: list[str] = []
    program = {"type": "program", "id": "program_item", "call_id": "parent", "code": "lookup()"}
    call = {
        "type": "function_call",
        "id": "fc_lookup",
        "call_id": "call_lookup",
        "name": "lookup",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "parent"},
    }
    program_result = {
        "type": "program_output",
        "id": "program_result",
        "call_id": "parent",
        "status": "completed",
        "result": "settled",
    }
    interim = {
        **program_result,
        "id": "program_interim",
        "status": "incomplete",
        "result": "working",
    }
    repeated = {key: value for key, value in interim.items() if key != "id"}
    answer = {
        "type": "message",
        "id": "msg_hosted",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "agent": {"agent_name": "/root"},
        "content": [{"type": "output_text", "text": "complete"}],
    }

    async def route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            requests.append(frame)
            if frame["type"] == "response.create":
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_hosted"}}
                )
                for index, item in enumerate((program, interim, call)):
                    await socket.send_json(
                        {"type": "response.output_item.done", "output_index": index, "item": item}
                    )
            else:
                await socket.send_json(
                    {"type": "response.inject.created", "response_id": "resp_hosted"}
                )
                await socket.send_json(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_hosted",
                            "status": "completed",
                            "store": stored,
                            "output": [
                                program,
                                interim,
                                call,
                                repeated,
                                repeated,
                                program_result,
                                answer,
                            ],
                        },
                    }
                )
        return socket

    @function_tool(allowed_callers=["programmatic"])
    def lookup() -> str:
        effects.append("selected")
        return "ok"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            backend = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(
                    instructions="Use the selected tool.",
                    model="gpt-5.5",
                    model_settings={"store": stored},
                    provider=provider,
                    tools=[lookup, HostedProgrammaticToolCalling()],
                )
                async with agent.session(observation_retention="all") as session:
                    turn = await session.submit("Call the tool.")
                    result = await turn.get_result()
                    assert result.final_output == "complete"
                    recorded = [
                        item
                        async for item in session.items()
                        if item["type"] in {"program", "program_output"}
                    ]

                    def observed_payload(payload: dict[str, Any]) -> dict[str, Any]:
                        if payload.get("id") in {
                            "program_item",
                            "program_interim",
                            "program_result",
                        }:
                            return payload
                        return {key: value for key, value in payload.items() if key != "id"}

                    expected = [program, interim, repeated, repeated, program_result]
                    assert [
                        observed_payload(item["provider_data"]) for item in recorded
                    ] == expected
                    completed_items = []
                    async for event in turn.events(include_execution_details=True):
                        if not isinstance(event, ModelOutputItemCompleted):
                            continue
                        item = event.item
                        if isinstance(item, dict) and item["type"] in ("program", "program_output"):
                            completed_items.append(item["provider_data"])
                    assert [observed_payload(item) for item in completed_items] == expected
    assert effects == ["selected"]
    assert [frame["type"] for frame in requests] == ["response.create", "response.inject"]
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_lookup"
        for item in requests[1]["input"]
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "positive",
        "positive-namespaced",
        "default-direct",
        "no-permission",
        "no-parent",
        "empty-parent",
        "closed-parent",
        "duplicate-parent",
        "no-builtin",
        "programmatic-only-direct",
        "code-only",
    ],
)
async def test_programmatic_call_requires_exact_native_owner(responses: Any, scenario: str) -> None:
    effects: list[str] = []
    namespaced = scenario == "positive-namespaced"
    positive = scenario in ("positive", "positive-namespaced", "default-direct")
    diagnostics = {
        "no-permission": "does not permit this provider caller",
        "no-parent": "has no live parent program",
        "empty-parent": "ended without complete supported output",
        "closed-parent": "has no unique active parent program",
        "duplicate-parent": "Invalid provider program call ID",
        "no-builtin": "require a provider programmatic tool declaration",
        "programmatic-only-direct": "does not permit this provider caller",
        "code-only": "requires an offered programmatic caller or tool search",
    }
    options: dict[str, Any] = {"namespace": "billing"} if namespaced else {}
    if scenario not in ("no-permission", "default-direct"):
        options["allowed_callers"] = ["code"] if scenario == "code-only" else ["programmatic"]

    @function_tool(**options)
    def lookup() -> str:
        """Return a synthetic value."""
        effects.append("selected")
        return "ok"

    program = {"type": "program", "id": "program_item", "call_id": "parent", "code": "lookup()"}
    completed = {
        "type": "program_output",
        "id": "program_output_item",
        "call_id": "parent",
        "status": "completed",
        "result": "done",
    }
    call: dict[str, Any] = {
        "type": "function_call",
        "id": "fc_lookup",
        "call_id": "call_lookup",
        "name": "lookup",
        "arguments": "{}",
    }
    if namespaced:
        call["namespace"] = "billing"
    if scenario not in ("default-direct", "programmatic-only-direct"):
        call["caller"] = {
            "type": "program",
            "caller_id": "" if scenario == "empty-parent" else "parent",
        }
    items: list[dict[str, Any]] = [] if scenario == "no-parent" else [program]
    if scenario == "closed-parent":
        items.append(completed)
    if scenario == "duplicate-parent":
        items.append(program.copy())
    responses.respond(
        *({"type": "response.output_item.done", "item": item} for item in [*items, call])
    )
    responses.respond(assistant_output("done"))
    provider = NativeProvider(
        base_url=responses.url,
        api_key="synthetic-programmatic",
        supports_websockets=False,
        request_max_retries=0,
        stream_max_retries=0,
    )
    tools: list[Any] = [lookup]
    if scenario != "no-builtin":
        tools.append(HostedProgrammaticToolCalling())
        if scenario in ("no-permission", "default-direct"):
            tools.append(
                CodeInterpreter(container="cntr_fixture", allowed_callers=["programmatic"])
            )
    async with (
        timeout(20),
        Agent(
            provider=provider, model="gpt-5.5", instructions="Use the selected tool.", tools=tools
        ).session() as session,
    ):
        turn = await session.submit("Call the tool.")
        if positive:
            assert (await turn.get_result()).final_output == "done"
            assert effects == ["selected"]
            assert len(responses.requests) == 2
            declarations: list[dict[str, Any]] = []
            for spec in responses.requests[0]["tools"]:
                declarations.extend(spec["tools"] if spec["type"] == "namespace" else [spec])
            if namespaced:
                assert any(
                    spec["type"] == "namespace" and spec["name"] == "billing"
                    for spec in responses.requests[0]["tools"]
                )
            spec = next(spec for spec in declarations if spec.get("name") == "lookup")
            assert spec.get("allowed_callers") == (
                None if scenario == "default-direct" else ["programmatic"]
            )
        else:
            with pytest.raises(NativeError, match=diagnostics[scenario]):
                await turn.get_result()
            assert effects == []
