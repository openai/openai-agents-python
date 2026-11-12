"""Borrowed OpenAI transport controls preserve the native model owner."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import APITimeoutError, AsyncOpenAI, omit

from agents import Agent, ChatCompletionsModel, ModelRequest, NativeProvider, ResponsesModel

ADAPTERS = [ChatCompletionsModel, ResponsesModel]


def request():
    return ModelRequest(
        body={
            "model": "fixture",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        },
        context={"key": "context"},
        session_id="session",
        turn_id="turn",
    )


def response(adapter):
    if adapter is ChatCompletionsModel:
        return {
            "id": "chat",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
        }
    return {
        "id": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
    }


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_borrowed_options_capture_maps_and_keep_transport_fields_out_of_body(adapter):
    seen = []

    async def handle(req):
        seen.append((dict(req.headers), list(req.query.items()), await req.json()))
        return web.json_response(response(adapter))

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="fixture",
            base_url=str(server.make_url("/v1/")),
            max_retries=0,
            default_headers={"x-remove": "inherited"},
        ) as client:
            options = {
                "headers": {"x-selected": "captured", "x-remove": None},
                "query": {"count": 2, "enabled": True, "tags": ["a", "b"]},
                "timeout_ms": 1000,
                "max_retries": 0,
            }
            model = adapter(client, request_options=options)
            options["headers"]["x-selected"] = "changed"
            options["query"]["tags"].append("changed")
            await model.complete(request())
            assert len(seen) == 1
            assert seen[0][0]["x-selected"] == "captured"
            assert "x-remove" not in seen[0][0]
            assert ("count", "2") in seen[0][1]
            assert ("enabled", "true") in seen[0][1]
            assert all(value != "changed" for _, value in seen[0][1])
            assert not {"headers", "query", "timeout_ms", "max_retries"} & seen[0][2].keys()
            assert not client.is_closed()


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_borrowed_options_factory_observes_detached_request_and_live_context(adapter):
    seen = []
    original = request()
    loop_thread = threading.get_ident()

    def select(observed):
        assert threading.get_ident() != loop_thread
        assert observed.context is original.context
        assert observed.session_id == original.session_id
        observed.body["model"] = "mutated"
        return {"headers": {"x-selected": observed.context["key"]}}

    async def handle(req):
        seen.append((req.headers["x-selected"], await req.json()))
        return web.json_response(response(adapter))

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="fixture", base_url=str(server.make_url("/v1/")), max_retries=0
        ) as client:
            await adapter(client, request_options=select).complete(original)
    assert original.body["model"] == "fixture"
    assert seen[0][0] == "context"
    assert seen[0][1]["model"] == "fixture"


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_borrowed_retry_override_keeps_original_client_open_and_counts_attempts(adapter):
    calls = 0

    async def handle(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response(
                {"error": {"message": "retry"}}, status=500, headers={"retry-after-ms": "1"}
            )
        return web.json_response(response(adapter))

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="fixture", base_url=str(server.make_url("/v1/")), max_retries=0
        ) as client:
            await adapter(client, request_options={"max_retries": 1}).complete(request())
            assert calls == 2
            assert client.max_retries == 0
            assert not client.is_closed()
            await adapter(client).complete(request())
            assert calls == 3
            assert not client.is_closed()


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_borrowed_per_attempt_timeout_uses_real_client_without_retries(adapter):
    read_timeouts = []

    def handle(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("fixture read deadline", request=request)

    async with AsyncOpenAI(
        api_key="fixture",
        base_url="https://provider.example/v1/",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        max_retries=2,
    ) as client:
        with pytest.raises(APITimeoutError):
            await adapter(client, request_options={"timeout_ms": 10, "max_retries": 0}).complete(
                request()
            )

    assert read_timeouts == [0.01]


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_options_cancellation_joins_sync_factory_before_any_client_call(adapter):
    entered = threading.Event()
    release = threading.Event()

    def select(_request):
        entered.set()
        release.wait(5)
        return {"headers": {"x-selected": "late"}}

    async with AsyncOpenAI(api_key="fixture") as client:
        pending = asyncio.create_task(adapter(client, request_options=select).complete(request()))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_request_options_reject_unknown_authority_fields(adapter):
    with pytest.raises(TypeError, match="Unsupported model request option"):
        adapter(object(), request_options={"signal": "override"})


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_options_failure_preserves_identity_and_releases_sdk_request_frames(adapter):
    failure = LookupError("factory failure")

    def select(observed):
        raise failure

    source = request()
    source.body["private"] = "synthetic-private-prompt"
    async with AsyncOpenAI(api_key="fixture") as client:
        with pytest.raises(LookupError) as caught:
            await adapter(client, request_options=select).complete(source)
    assert caught.value is failure
    trace = failure.__traceback__
    while trace is not None:
        if "/_agents_codex/" in trace.tb_frame.f_code.co_filename:
            values = trace.tb_frame.f_locals
            assert not {
                "request",
                "observed",
                "options",
                "selected",
                "body",
                "messages",
            }.intersection(values)
            assert not values.get("args")
            assert not values.get("kwargs")
        trace = trace.tb_next


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_options_reject_invalid_query_without_retaining_encoder_payload(adapter):
    async def select(_observed):
        return {"query": {"private": "synthetic-private-prompt", "invalid": object()}}

    async with AsyncOpenAI(api_key="fixture") as client:
        with pytest.raises(ValueError, match="JSON values") as caught:
            await adapter(client, request_options=select).complete(request())
    assert caught.value.__context__ is None
    trace = caught.value.__traceback__
    while trace is not None:
        if "/_agents_codex/" in trace.tb_frame.f_code.co_filename:
            assert "synthetic-private-prompt" not in repr(trace.tb_frame.f_locals)
        trace = trace.tb_next


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_native_attempt_retry_directive_overrides_factory_without_mutating_borrowed_client(
    adapter,
):
    from dataclasses import replace

    from openai import InternalServerError

    seen = []
    captured = []

    async def handle(req):
        seen.append(await req.json())
        return web.json_response(
            {"error": {"message": "retryable", "type": "server_error"}}, status=500
        )

    def options(value):
        captured.append(
            (
                value.attempt_id,
                value.attempt,
                value.retry_configured,
                value.disable_provider_retries,
            )
        )
        return {"max_retries": 2}

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="fixture", base_url=str(server.make_url("/v1/")), max_retries=1
        ) as client:
            model = adapter(client, request_options=options)
            current = replace(
                request(),
                attempt_id="attempt-2",
                attempt=2,
                retry_configured=True,
                disable_provider_retries=True,
            )
            with pytest.raises(InternalServerError):
                await model.complete(current)
            assert len(seen) == 1
            assert captured == [("attempt-2", 2, True, True)]
            assert client.max_retries == 1
            assert (
                not {"attempt_id", "attempt", "retry_configured", "disable_provider_retries"}
                & seen[0].keys()
            )


def test_model_request_measurement_is_exact_attempt_local_and_preserves_positional_prefix():
    from dataclasses import replace

    measured = []
    current = replace(
        request(),
        attempt_id="attempt-1",
        _report_usage=lambda value: measured.append(value) or True,
    )
    assert current.report_usage({"input_tokens": 3, "output_tokens": 2})
    assert measured == [
        {
            "input_tokens": 3,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 0,
            "total_tokens": 5,
        }
    ]
    assert not request().report_usage({"input_tokens": 1})
    with pytest.raises(ValueError):
        current.report_usage({"total_tokens": True})
    assert len(measured) == 1


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("route", ["adapter", "native"])
async def test_scoped_transport_overlays_borrowed_client_options(adapter, route):
    seen = []

    async def handle(req):
        seen.append(
            (
                {name.lower(): value for name, value in req.headers.items()},
                dict(req.query),
                await req.json(),
            )
        )
        return web.json_response(response(adapter))

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    scoped = {
        "headers": {"x-routing": "operation", "x-drop": None},
        "query": {"route": "operation", "drop": None, "count": 2},
    }
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-borrowed",
            base_url=str(server.make_url("/v1/")),
            max_retries=0,
            default_headers={"x-drop": "client"},
            default_query={"keep": "client", "drop": "client"},
        ) as client:
            model = adapter(
                client,
                request_options={
                    "headers": {"X-Routing": "adapter", "x-adapter": "retained"},
                    "query": {"route": "adapter"},
                },
            )
            if route == "adapter":
                operation = request()
                object.__setattr__(operation, "_request_transport", scoped)
                await model.complete(operation)
                await model.complete(request())
            else:
                async with NativeProvider(
                    api_key="synthetic-native",
                    base_url="https://unused.invalid/v1",
                    model_backend=model,
                ) as provider:
                    agent = Agent(model="gpt-4.1", instructions="Answer.", provider=provider)
                    async with agent.session() as session:
                        first = await session.submit(
                            "Scoped.",
                            model_settings_override={
                                "extra_headers": {
                                    key: omit if value is None else value
                                    for key, value in scoped["headers"].items()
                                },
                                "extra_query": scoped["query"],
                            },
                        )
                        assert (await first.get_result()).final_output == "ok"
                        second = await session.submit("Defaults.")
                        assert (await second.get_result()).final_output == "ok"

    assert len(seen) == 2
    first_headers, first_query, first_body = seen[0]
    second_headers, second_query, second_body = seen[1]
    assert (
        first_headers["authorization"]
        == second_headers["authorization"]
        == "Bearer synthetic-borrowed"
    )
    assert first_headers["x-routing"] == "operation"
    assert first_headers["x-adapter"] == "retained"
    assert "x-drop" not in first_headers
    assert first_query == {"route": "operation", "count": "2", "keep": "client"}
    assert second_headers["x-routing"] == "adapter"
    assert second_headers["x-drop"] == "client"
    assert second_query == {"route": "adapter", "keep": "client", "drop": "client"}
    assert all("request_transport" not in body for body in (first_body, second_body))
