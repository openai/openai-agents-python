from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI
from pydantic import BaseModel

from agents import (
    Agent,
    ChatCompletionsModel,
    FunctionTool,
    ModelRetrySettings,
    NativeProvider,
    PromptedOutput,
    ResponsesModel,
    RetryDecision,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_websockets import WebSocketPeer, peer


async def test_request_override_stays_on_independent_websocket_endpoint():
    async with (
        timeout(30),
        peer(base_path="/http/provider") as http,
        peer(base_path="/socket/provider") as websocket,
        NativeProvider(
            base_url=http.url + "?origin=http",
            websocket_base_url=websocket.url.replace("http://", "ws://", 1) + "?origin=socket",
            api_key="synthetic-transport-owner",
            supports_websockets=True,
        ) as provider,
        Agent(instructions="Answer briefly.", provider=provider).session() as session,
    ):
        for label in ("first", "second"):
            websocket.respond(assistant_output(label))
            turn = await session.submit(
                "Answer.",
                model_settings_override={
                    "extra_headers": {"x-operation": label},
                    "extra_query": {"operation": label},
                },
            )
            assert (await turn.get_result()).final_output == label

        assert not http.request_paths
        admitted = [
            (urlsplit(target), headers["x-operation"])
            for (_, target), headers in zip(
                websocket.request_paths, websocket.request_headers, strict=True
            )
            if "x-operation" in headers
        ]
        assert [label for _, label in admitted] == ["first", "second"]
        for target, label in admitted:
            assert target.path == "/socket/provider/responses"
            assert parse_qs(target.query) == {"origin": ["socket"], "operation": [label]}


@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("capture", ["save_state", "fork"])
async def test_operation_transport_is_not_checkpoint_authority(monkeypatch, websocket, capture):
    model = "gpt-5.1-codex" if websocket else "fixture-unlisted-request-controls"
    observed: list[tuple[dict[str, str], dict[str, str]]] = []
    method = "websocket" if websocket else "handle"
    original = getattr(WebSocketPeer, method)

    async def record(self, request):
        observed.append(
            ({name.lower(): value for name, value in request.headers.items()}, dict(request.query))
        )
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, method, record)

    @function_tool
    def lookup(value: str) -> str:
        """Return one synthetic lookup result."""
        return value

    async with timeout(30), peer() as server:
        provider = server.provider(websocket=websocket)

        def inference_routes():
            if not websocket:
                return observed
            return [
                (headers, next(query for upgrade, query in observed if upgrade == headers))
                for headers in server.request_headers
            ]

        source = Agent(
            model=model,
            instructions="Use lookup once, then answer.",
            provider=provider,
            tools=[lookup],
            model_settings={
                "extra_headers": {"x-default": "SYNTHETIC_SOURCE_DEFAULT"},
                "extra_query": {"default": "SYNTHETIC_SOURCE_QUERY"},
            },
        )
        server.respond(function_call("lookup", {"value": "receipt"}, "lookup-source"))
        server.respond(assistant_output("source answer"))
        async with source.session() as session:
            turn = await session.submit(
                "First operation.",
                model_settings_override={
                    **({} if websocket else {"temperature": 0.2}),
                    "extra_headers": {"x-operation": "SYNTHETIC_FIRST_OPERATION"},
                    "extra_query": {"operation": "SYNTHETIC_FIRST_QUERY"},
                },
            )
            assert (await turn.get_result()).final_output == "source answer"
            if websocket:
                server.respond(assistant_output("same scoped socket"))
                await (
                    await session.submit(
                        "Use the same explicitly granted route once more.",
                        model_settings_override={
                            "extra_headers": {"x-operation": "SYNTHETIC_FIRST_OPERATION"},
                            "extra_query": {"operation": "SYNTHETIC_FIRST_QUERY"},
                        },
                    )
                ).get_result()
                generated = [frame for frame in server.frames if frame.get("generate") is not False]
                assert generated[-1].get("previous_response_id")
            state = await getattr(session, capture)()
            encoded = state.to_json()
            for marker in (
                "SYNTHETIC_SOURCE_DEFAULT",
                "SYNTHETIC_SOURCE_QUERY",
                "SYNTHETIC_FIRST_OPERATION",
                "SYNTHETIC_FIRST_QUERY",
            ):
                assert marker not in encoded
            if websocket:
                warmup_headers, warmup_query = observed[0]
                assert warmup_headers.get("x-default") == "SYNTHETIC_SOURCE_DEFAULT"
                assert warmup_query == {"default": "SYNTHETIC_SOURCE_QUERY"}
            first_routes = len(inference_routes())
            assert first_routes
            for headers, query in inference_routes():
                assert "x-default" not in headers
                assert headers["x-operation"] == "SYNTHETIC_FIRST_OPERATION"
                assert query["operation"] == "SYNTHETIC_FIRST_QUERY"
                assert "default" not in query
            server.respond(assistant_output("next live answer"))
            assert (
                await (await session.submit("Next live operation.")).get_result()
            ).final_output == ("next live answer")
            assert len(inference_routes()) > first_routes
            for headers, query in inference_routes()[first_routes:]:
                assert headers["x-default"] == "SYNTHETIC_SOURCE_DEFAULT"
                assert "x-operation" not in headers
                assert "operation" not in query
            assert "SYNTHETIC_FIRST_OPERATION" not in (await session.save_state()).to_json()

        current = Agent(
            model=model,
            instructions="Use lookup once, then answer.",
            provider=provider,
            tools=[lookup],
            model_settings={
                "extra_headers": {"x-default": "SYNTHETIC_DESTINATION_DEFAULT"},
                "extra_query": {"default": "SYNTHETIC_DESTINATION_QUERY"},
            },
        )
        prior = len(inference_routes())
        server.respond(assistant_output("restored answer"))
        async with current.session(state=SessionState.from_json(encoded)) as restored:
            assert (
                await (await restored.submit("Continue from the saved operation.")).get_result()
            ).final_output == "restored answer"
        assert len(inference_routes()) > prior
        for headers, query in inference_routes()[prior:]:
            assert headers["x-default"] == "SYNTHETIC_DESTINATION_DEFAULT"
            assert "x-operation" not in headers
            assert query["default"] == "SYNTHETIC_DESTINATION_QUERY"
            assert "operation" not in query
        assert "SYNTHETIC_FIRST_OPERATION" not in json.dumps(server.requests)


async def test_queued_transport_is_detached_and_steering_cannot_replace_it(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    queries = []
    original = WebSocketPeer.handle

    async def record(self, request):
        queries.append(dict(request.query))
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, "handle", record)

    @function_tool
    async def wait_for_release() -> str:
        """Wait on the test-owned release barrier."""
        entered.set()
        await release.wait()
        return "released"

    async with timeout(30), peer() as server:
        provider = server.provider(websocket=False)
        server.respond(function_call("wait_for_release", {}, "held-source"))
        server.respond(assistant_output("released source"))
        server.respond(assistant_output("queued answer"))
        server.respond(assistant_output("ordinary answer"))
        agent = Agent(instructions="Answer.", tools=[wait_for_release], provider=provider)
        async with agent.session() as session:
            first = await session.submit("Wait.")
            await entered.wait()
            headers, query = {"x-queued": "SYNTHETIC_QUEUED_CAPTURE"}, {"queued": "captured"}
            queued = await session.submit(
                "Queued work.",
                if_busy="queue",
                model_settings_override={"extra_headers": headers, "extra_query": query},
            )
            headers["x-queued"] = "SYNTHETIC_LATE_MUTATION"
            query["queued"] = "mutated"
            with pytest.raises(TypeError, match="steer"):
                await cast(Any, session).submit(
                    "Cannot replace the active transport.",
                    if_busy="steer",
                    model_settings_override={},
                )
            release.set()
            assert (await first.get_result()).final_output == "released source"
            assert (await queued.get_result()).final_output == "queued answer"
            assert server.request_headers[-1]["x-queued"] == "SYNTHETIC_QUEUED_CAPTURE"
            assert queries[-1]["queued"] == "captured"
            assert (
                await (await session.submit("Ordinary work.")).get_result()
            ).final_output == "ordinary answer"
            assert "x-queued" not in server.request_headers[-1]
            assert "queued" not in queries[-1]
            assert "SYNTHETIC_QUEUED_CAPTURE" not in (await session.save_state()).to_json()


@pytest.mark.parametrize("adapter", [ResponsesModel, ChatCompletionsModel])
async def test_borrowed_transport_and_async_credentials_are_request_scoped(adapter):
    observed: list[tuple[str | None, dict[str, str], dict[str, Any]]] = []
    keys = iter(["SYNTHETIC_FIRST_KEY", "SYNTHETIC_SECOND_KEY"])

    async def api_key() -> str:
        return next(keys)

    async def handle(request):
        body = await request.json()
        assert "request_transport" not in body
        observed.append(
            (
                request.headers.get("authorization"),
                dict(request.query),
                {name.lower(): value for name, value in request.headers.items()},
            )
        )
        if adapter is ChatCompletionsModel:
            return web.json_response(
                {
                    "id": "chat-synthetic",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "answer"},
                        }
                    ],
                }
            )
        return web.json_response(
            {
                "id": f"resp_synthetic_{len(observed)}",
                "status": "completed",
                "output": [assistant_output("answer")["item"]],
            }
        )

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with timeout(30), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key=api_key, base_url=str(server.make_url("/v1/")), max_retries=0
        ) as client:
            backend = adapter(client)
            async with NativeProvider(
                base_url="https://unused.invalid/v1", model_backend=backend
            ) as provider:
                agent = Agent(
                    instructions="Answer.",
                    model="gpt-4.1",
                    provider=provider,
                    model_settings={"extra_headers": {"x-default": "SYNTHETIC_DEFAULT"}},
                )
                async with agent.session() as session:
                    assert (
                        await (
                            await session.submit(
                                "First operation.",
                                model_settings_override={
                                    "extra_headers": {"x-operation": "SYNTHETIC_ADAPTER_OPERATION"},
                                    "extra_query": {"operation": "first"},
                                },
                            )
                        ).get_result()
                    ).final_output == "answer"
                    saved = (await session.save_state()).to_json()
                    assert (
                        await (await session.submit("Second operation.")).get_result()
                    ).final_output == "answer"
                assert not client.is_closed()
    assert len(observed) == 2
    assert [headers for headers, _, _ in observed] == [
        "Bearer SYNTHETIC_FIRST_KEY",
        "Bearer SYNTHETIC_SECOND_KEY",
    ]
    assert observed[0][1] == {"operation": "first"}
    assert observed[0][2]["x-operation"] == "SYNTHETIC_ADAPTER_OPERATION"
    assert observed[1][1] == {}
    assert "x-operation" not in observed[1][2]
    assert "x-default" not in observed[0][2]
    assert observed[1][2]["x-default"] == "SYNTHETIC_DEFAULT"
    for marker in ("SYNTHETIC_FIRST_KEY", "SYNTHETIC_SECOND_KEY", "SYNTHETIC_ADAPTER_OPERATION"):
        assert marker not in saved


@pytest.mark.parametrize("stage_input", [False, True], ids=["manual", "staged-input"])
async def test_manual_resume_retains_sampling_but_rebinds_transport(monkeypatch, stage_input):
    observed = []
    original = WebSocketPeer.handle

    async def record(self, request):
        observed.append(
            ({name.lower(): value for name, value in request.headers.items()}, dict(request.query))
        )
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, "handle", record)
    lookup = FunctionTool(
        name="lookup", description="Read one value.", input_type=LookupArgs, output_type=str
    )
    async with timeout(30), peer() as server:
        provider = server.provider(websocket=False)
        source = Agent(
            model="fixture-unlisted-request-controls",
            instructions="Look up a value.",
            provider=provider,
            tools=[lookup],
            model_settings={"temperature": 0.6},
        )
        server.respond(function_call("lookup", {"key": "record"}, "manual-record"))
        async with source.session() as session:
            await session.submit(
                "Look up record.",
                model_settings_override={
                    "temperature": 0.2,
                    "extra_headers": {"x-operation": "SYNTHETIC_MANUAL_SOURCE"},
                    "extra_query": {"operation": "SYNTHETIC_MANUAL_QUERY"},
                },
            )
            await next_request(session.interactions(), lookup)
            state = await session.suspend()
        staged_text = "Keep this staged input across restore."
        if stage_input:
            state = state.with_pending_input(staged_text)
        encoded = state.to_json()
        assert "SYNTHETIC_MANUAL_SOURCE" not in encoded
        assert "SYNTHETIC_MANUAL_QUERY" not in encoded
        current = Agent(
            model="fixture-unlisted-request-controls",
            instructions="Look up a value.",
            provider=provider,
            tools=[lookup],
            model_settings={
                "temperature": 0.6,
                "extra_headers": {"x-current": "SYNTHETIC_MANUAL_CURRENT"},
            },
        )
        server.respond(assistant_output("resumed result"))
        server.respond(assistant_output("independent result"))
        async with current.session(state=SessionState.from_json(encoded)) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("receipt")
            assert restored.restored_turn is not None
            assert (await restored.restored_turn.get_result()).final_output == "resumed result"
            assert server.requests[-1]["temperature"] == 0.2
            if stage_input:
                assert staged_text in json.dumps(server.requests[-1]["input"])
            assert observed[-1][0]["x-current"] == "SYNTHETIC_MANUAL_CURRENT"
            assert "x-operation" not in observed[-1][0]
            assert "operation" not in observed[-1][1]
            assert (
                await (await restored.submit("Independent operation.")).get_result()
            ).final_output == ("independent result")
            assert server.requests[-1]["temperature"] == 0.6


class RepairTransportReport(BaseModel):
    label: str


@pytest.mark.parametrize(
    ("handoff", "override", "updated", "root_updated"),
    [
        (False, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, True, True, False),
        (True, False, False, False),
        (True, True, False, False),
        (True, False, True, False),
        (True, True, True, False),
        (True, False, False, True),
    ],
)
async def test_prompted_repair_preserves_active_transport(
    monkeypatch, handoff, override, updated, root_updated
):
    observed = []
    original = WebSocketPeer.handle

    async def record(self, request):
        observed.append(
            ({name.lower(): value for name, value in request.headers.items()}, dict(request.query))
        )
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, "handle", record)

    def settings(value):
        return {
            "extra_headers": {"x-agent-route": value},
            "extra_query": {"agent_route": value},
        }

    async with timeout(30), peer() as server:
        provider = server.provider(websocket=False)
        worker = Agent(
            name="Worker",
            instructions="Produce the report.",
            model="gpt-5.5",
            provider=provider,
            model_settings=settings("SYNTHETIC_WORKER"),
        )
        root = Agent(
            instructions="Produce the report, handing off if requested.",
            model="gpt-5.5",
            provider=provider,
            model_settings=settings("SYNTHETIC_ROOT"),
            handoffs=[worker] if handoff else [],
        )
        async with root.session() as session:
            if root_updated:
                await session.update(model_settings=settings("SYNTHETIC_ROOT_UPDATED"))
            if handoff:
                server.respond(function_call("transfer_to_worker", {}, "repair-handoff"))
                server.respond(assistant_output("Worker ready."))
                assert (await (await session.submit("Use Worker.")).get_result()).final_output
            if updated:
                latest = settings("SYNTHETIC_UPDATED")
                await session.update(model_settings={"extra_headers": latest["extra_headers"]})
                await session.update(model_settings={"extra_query": latest["extra_query"]})
            first_report = len(observed)
            server.respond(assistant_output("invalid JSON"))
            server.respond(assistant_output('{"label":"corrected"}'))
            operation = (
                {
                    "extra_headers": {"x-operation": "SYNTHETIC_OPERATION"},
                    "extra_query": {"operation": "SYNTHETIC_OPERATION"},
                }
                if override
                else None
            )
            turn = await session.submit(
                "Produce the report.",
                output_type=PromptedOutput(RepairTransportReport, 1),
                model_settings_override=operation,
            )
            result = await turn.get_result()
            assert result.final_output == RepairTransportReport(label="corrected")
            assert len(result.repair_attempts) == 1
        assert len(observed) - first_report == 2
        for headers, query in observed[first_report:]:
            if override:
                assert headers["x-operation"] == "SYNTHETIC_OPERATION"
                assert "x-agent-route" not in headers
                assert query == {"operation": "SYNTHETIC_OPERATION"}
            else:
                expected = (
                    "SYNTHETIC_UPDATED"
                    if updated
                    else "SYNTHETIC_WORKER"
                    if handoff
                    else "SYNTHETIC_ROOT"
                )
                assert headers["x-agent-route"] == expected
                assert query == {"agent_route": expected}


async def test_prompted_repair_keeps_the_operation_retry_policy_owner(monkeypatch):
    request_count = 0
    retry_calls = []
    original = WebSocketPeer.handle

    async def fail_first_repair(self, request):
        nonlocal request_count
        request_count += 1
        if request_count == 2:
            return web.json_response(
                {"error": {"type": "server_error", "message": "synthetic repair failure"}},
                status=503,
            )
        return await original(self, request)

    def retry(context):
        retry_calls.append(context)
        return RetryDecision(True, delay_ms=0)

    monkeypatch.setattr(WebSocketPeer, "handle", fail_first_repair)

    async with timeout(30), peer() as server:
        server.respond(assistant_output("invalid JSON"))
        server.respond(assistant_output('{"label":"corrected"}'))
        provider = NativeProvider(
            base_url=server.url,
            api_key="synthetic-repair-retry-owner",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        )
        agent = Agent(
            instructions="Produce the report.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=0, policy=retry)},
        )
        async with provider, agent.session() as session:
            turn = await session.submit(
                "Produce the report.",
                output_type=PromptedOutput(RepairTransportReport, 1),
                model_settings_override={
                    "retry": ModelRetrySettings(max_retries=1, policy=retry),
                },
            )
            result = await turn.get_result()
            assert result.final_output == RepairTransportReport(label="corrected")
            assert len(result.repair_attempts) == 1

    assert request_count == 3
    assert len(retry_calls) == 1


@pytest.mark.parametrize("restore", [False, True])
async def test_operation_retry_policy_keeps_its_owner_after_handoff(monkeypatch, restore):
    request_count = 0
    retry_calls = []
    original = WebSocketPeer.handle

    async def fail_first_worker_request(self, request):
        nonlocal request_count
        request_count += 1
        if request_count == (3 if restore else 2):
            return web.json_response(
                {"error": {"type": "server_error", "message": "synthetic worker failure"}},
                status=503,
            )
        return await original(self, request)

    def retry(context):
        retry_calls.append(context)
        return RetryDecision(True, delay_ms=0)

    monkeypatch.setattr(WebSocketPeer, "handle", fail_first_worker_request)

    async with timeout(30), peer() as server:
        lookup = FunctionTool(
            name="lookup", description="Look up a record.", input_type=LookupArgs, output_type=str
        )
        server.respond(function_call("transfer_to_worker", {}, "handoff"))
        if restore:
            server.respond(function_call("lookup", {"key": "record"}, "manual"))
        server.respond(assistant_output("Worker finished."))
        provider = NativeProvider(
            base_url=server.url,
            api_key="synthetic-handoff-retry-owner",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=0,
        )
        worker = Agent(
            name="Worker", instructions="Finish the task.", tools=[lookup] if restore else []
        )
        root = Agent(
            name="Root",
            instructions="Transfer the task.",
            provider=provider,
            handoffs=[worker],
            model_settings={"retry": ModelRetrySettings(max_retries=0, policy=retry)},
        )
        async with provider:
            async with root.session() as session:
                turn = await session.submit(
                    "Start the task.",
                    model_settings_override={
                        "retry": ModelRetrySettings(max_retries=1, policy=retry),
                    },
                )
                if restore:
                    await next_request(session.interactions(), lookup)
                    saved = SessionState.from_json((await session.suspend()).to_json())
                else:
                    assert (await turn.get_result()).final_output == "Worker finished."
            if restore:
                async with root.session(state=saved) as session:
                    request = await next_request(session.interactions(), lookup)
                    turn = session.active_turn
                    assert turn is not None
                    await request.respond("found")
                    assert (await turn.get_result()).final_output == "Worker finished."

    assert request_count == (4 if restore else 3)
    assert len(retry_calls) == 1
