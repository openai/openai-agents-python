"""Native provider replay fences and captured provider safety advice."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import APIStatusError, AsyncOpenAI

from agents import (
    Agent,
    ModelResponse,
    ModelRetrySettings,
    NativeError,
    NativeProvider,
    ResponsesModel,
    RetryAdvice,
    RetryBackoffSettings,
    RetryDecision,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output

pytestmark = pytest.mark.asyncio


def settings(configured, decisions):
    def policy(context):
        decisions.append(context)
        return RetryDecision(True, delay_ms=0, approve_unsafe_replay=True)

    return (
        {
            "retry": ModelRetrySettings(
                max_retries=1,
                backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0, jitter=False),
                policy=policy,
            )
        }
        if configured
        else {}
    )


@asynccontextmanager
async def peer(frames, *, effectful=False):
    attempts, effects = [], []
    model = ResponsesFixture()
    model.respond(assistant_output("recovered"))

    async def handle(request):
        attempts.append(await request.json())
        if effectful:
            effects.append("provider-effect")
        if len(attempts) == 1:
            return web.Response(
                text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
                content_type="text/event-stream",
            )
        return await model.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app, shutdown_timeout=1) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-hosted-retry",
            supports_websockets=False,
            request_max_retries=0,
            stream_max_retries=1,
        ) as provider:
            yield provider, attempts, effects, model


def created():
    return {"type": "response.created", "response": {"id": "started"}}


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("kind", ["code_interpreter_call", "image_generation_call", "mcp_call"])
@pytest.mark.parametrize("phase", ["added", "done", "progress"])
async def test_observed_hosted_effect_cannot_replay(configured, kind, phase):
    item = {
        "type": kind,
        "id": "provider-action",
        "status": "in_progress",
        "name": "mutate",
        "server_label": "remote",
        "arguments": "{}",
    }
    event = (
        {"type": f"response.{kind}.in_progress", "item_id": item["id"], "output_index": 0}
        if phase == "progress"
        else {"type": f"response.output_item.{phase}", "output_index": 0, "item": item}
    )
    decisions = []
    async with (
        timeout(20),
        peer([created(), event], effectful=True) as (
            provider,
            attempts,
            effects,
            _,
        ),
    ):
        agent = Agent(
            model="gpt-5.5",
            instructions="Use the provider tool.",
            hosted_prompt={"id": "pmpt_provider_tools"},
            provider=provider,
            model_settings=settings(configured, decisions),
        )
        with pytest.raises(NativeError):
            await agent.run("run")
    assert len(attempts) == 1
    assert effects == ["provider-effect"]
    assert not decisions


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.shell_call_command.delta",
            "item_id": "shell-command",
            "output_index": 0,
            "command_index": 0,
            "delta": "test command",
        },
        {
            "type": "response.shell_call_output_content.delta",
            "item_id": "shell-receipt",
            "output_index": 0,
            "content_index": 0,
            "delta": {"stdout": "test output", "stderr": ""},
        },
    ],
)
async def test_hosted_shell_progress_fences_replay_with_an_explicit_empty_catalog(event):
    decisions = []
    async with (
        timeout(20),
        peer([created(), event], effectful=True) as (provider, attempts, effects, _),
    ):
        agent = Agent(
            instructions="Answer.",
            hosted_prompt={"id": "pmpt_provider_tools"},
            tools=[],
            provider=provider,
            model_settings=settings(True, decisions),
        )
        with pytest.raises(NativeError):
            await agent.run("run")

    assert len(attempts) == 1
    assert attempts[0]["tools"] == []
    assert effects == ["provider-effect"]
    assert not decisions


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("mode", ["accepted", "no_created", "explicit_empty"])
async def test_opaque_dispatch_requires_safety_even_before_created(configured, mode):
    frames = [] if mode == "no_created" else [created()]
    decisions = []
    async with timeout(20), peer(frames) as (provider, attempts, _, model):
        agent = Agent(
            instructions="Answer.",
            hosted_prompt={"id": "pmpt_provider_tools"},
            provider=provider,
            model_settings=settings(configured, decisions),
            **({"tools": []} if mode == "explicit_empty" else {}),
        )
        if mode == "explicit_empty":
            assert (await agent.run("run")).final_output == "recovered"
            assert len(attempts) == 2
            assert attempts[0]["tools"] == []
            assert not model.replies
        else:
            with pytest.raises(NativeError):
                await agent.run("run")
            assert len(attempts) == 1


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("kind", ["none", "file_search_call", "web_search_call", "reasoning"])
async def test_explicit_readonly_catalog_retains_retry(configured, kind):
    frames = [created()]
    if kind != "none":
        item = {"type": kind, "id": "read-only"}
        if kind == "reasoning":
            item["summary"] = []
        elif kind == "web_search_call":
            item.update(status="completed", action={"type": "search", "query": "A"})
        else:
            item.update(status="completed", results=[])
        frames.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    decisions = []
    async with timeout(20), peer(frames) as (provider, attempts, _, model):
        agent = Agent(
            instructions="Answer.",
            hosted_prompt={"id": "pmpt_read_only"},
            tools=[],
            provider=provider,
            model_settings=settings(configured, decisions),
        )
        assert (await agent.run("run")).final_output == "recovered"
        assert len(attempts) == 2
        assert attempts[0]["tools"] == []
        assert not model.replies


@pytest.mark.parametrize("safety", ["safe", "unknown", "unsafe"])
async def test_only_captured_provider_advice_can_release_opaque_risk(safety):
    calls, decisions = [], []
    original = RuntimeError("opaque provider failure")
    original.status_code = 503
    original.code = "server_error"

    class Model:
        def get_retry_advice(self, request):
            assert request.error is original
            return RetryAdvice(suggested=True, replay_safety=safety)

        def complete(self, request):
            calls.append(request)
            if len(calls) == 1:
                raise original
            return ModelResponse([assistant_output("recovered")["item"]])

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            instructions="Answer.",
            hosted_prompt={"id": "pmpt_provider_tools"},
            provider=provider,
            model_settings=settings(True, decisions),
        )
        if safety == "safe":
            assert (await agent.run("run")).final_output == "recovered"
            assert len(calls) == 2
        else:
            with pytest.raises(RuntimeError) as stopped:
                await agent.run("run")
            assert stopped.value is original
            assert len(calls) == 1


@pytest.mark.parametrize("mode", ["hosted", "empty", "plain"])
async def test_borrowed_provider_bounds_first_dispatch_without_mutating_client(mode):
    requests = []

    async def send(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            503,
            json={"error": {"message": "temporary"}},
            headers={"retry-after-ms": "0"},
        )

    async def options(_request):
        return {"max_retries": 4}

    async with (
        timeout(20),
        httpx.AsyncClient(transport=httpx.MockTransport(send)) as http_client,
        AsyncOpenAI(
            api_key="synthetic-inner-retry",
            base_url="https://fixture.invalid/v1",
            max_retries=2,
            http_client=http_client,
        ) as client,
    ):
        async with NativeProvider(
            model_backend=ResponsesModel(
                client, request_options=options if mode == "hosted" else None
            )
        ) as provider:
            selected = (
                {"model": "gpt-5.5"}
                if mode == "plain"
                else {"hosted_prompt": {"id": "pmpt_inner_retry"}}
            )
            if mode == "empty":
                selected["tools"] = []
            agent = Agent(
                instructions="Answer.",
                provider=provider,
                model_settings={
                    "retry": ModelRetrySettings(
                        max_retries=1,
                        backoff=RetryBackoffSettings(initial_delay_ms=0, jitter=False),
                        policy=lambda _: RetryDecision(False),
                    )
                },
                **selected,
            )
            with pytest.raises(APIStatusError):
                await agent.run("run")
            assert len(requests) == (1 if mode == "hosted" else 3)
            assert client.max_retries == 2
            if mode == "empty":
                assert all(request["tools"] == [] for request in requests)
        assert not http_client.is_closed


@pytest.mark.parametrize("started", [False, True])
async def test_explicit_hosted_mcp_before_effect_frame_needs_safe_advice(started):
    from pathlib import Path

    from agents import HostedMCP

    catalog = Path(__file__).parent / "fixtures" / "request-control-models.json"
    model = next(
        entry["slug"]
        for entry in json.loads(catalog.read_text())["models"]
        if entry.get("supported_in_api")
        and not entry.get("use_responses_lite")
        and entry.get("tool_mode") != "code_mode_only"
    )
    declaration = HostedMCP(
        tool_config={
            "type": "mcp",
            "server_label": "remote",
            "server_url": "https://hosted-mcp.invalid/remote",
            "require_approval": "never",
        }
    )
    decisions = []
    async with (
        timeout(20),
        peer([created()] if started else [], effectful=True) as (provider, attempts, effects, _),
    ):
        agent = Agent(
            model=model,
            instructions="Use the provider tool.",
            tools=[declaration],
            provider=provider,
            model_settings=settings(True, decisions),
        )
        failure = None
        try:
            await agent.run("run")
        except NativeError as error:
            failure = error
    assert len(attempts) == 1
    assert effects == ["provider-effect"]
    assert failure is not None
    assert attempts[0]["tools"][0]["type"] == "mcp"
    assert attempts[0]["tools"][0]["server_label"] == "remote"


@pytest.mark.parametrize("safety", ["safe", "unknown", "unsafe"])
async def test_explicit_hosted_mcp_replay_needs_captured_safe_advice(safety):
    from pathlib import Path

    from agents import HostedMCP

    catalog = Path(__file__).parent / "fixtures" / "request-control-models.json"
    model = next(
        entry["slug"]
        for entry in json.loads(catalog.read_text())["models"]
        if entry.get("supported_in_api")
        and not entry.get("use_responses_lite")
        and entry.get("tool_mode") != "code_mode_only"
    )
    declaration = HostedMCP(
        tool_config={
            "type": "mcp",
            "server_label": "remote",
            "server_url": "https://hosted-mcp.invalid/remote",
            "require_approval": "never",
        }
    )
    calls, decisions = [], []
    original = RuntimeError("opaque explicit MCP dispatch")
    original.status_code = 503
    original.code = "server_error"

    class Model:
        def get_retry_advice(self, request):
            assert request.error is original
            return RetryAdvice(suggested=True, replay_safety=safety)

        def complete(self, request):
            calls.append(request)
            if len(calls) == 1:
                raise original
            return ModelResponse([assistant_output("recovered")["item"]])

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            model=model,
            instructions="Answer.",
            tools=[declaration],
            provider=provider,
            model_settings=settings(True, decisions),
        )
        if safety == "safe":
            assert (await agent.run("run")).final_output == "recovered"
            assert len(calls) == 2
        else:
            with pytest.raises(RuntimeError) as stopped:
                await agent.run("run")
            assert stopped.value is original
            assert len(calls) == 1
