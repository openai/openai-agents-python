"""Real Responses rounds preserve caller IDs in processors and OTLP."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.telemetry_transport_fixtures import decoded_collector
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder, _descends, _spans

from agents import Agent, NativeProvider, TurnStoppedError, function_tool, telemetry


@pytest.mark.parametrize("case", ["rounds", "invalid", "cancel", "retry", "cancel-io"])
def test_provider_response_span_native_admission(case):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_provider_response_span_integration import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            case,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS response span {case}" in result.stdout


async def native_case(case):
    fixture = ResponsesFixture()
    effects = []
    tool_span_ids = []

    @function_tool
    def echo() -> str:
        context = telemetry.current_context()
        assert context is not None
        tool_span_ids.append(context.span_id)
        effects.append("echo")
        return "done"

    if case == "rounds":
        fixture.respond(function_call("echo", {}, "call-one"))
    fixture.respond(assistant_output("Complete."))
    app = web.Application()
    attempts = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        attempts.append(request.path)
        if case == "retry" and len(attempts) == 1:
            await request.json()
            return web.json_response(
                {"error": {"message": "Synthetic transient failure", "type": "server_error"}},
                status=503,
                headers={"retry-after-ms": "0"},
            )
        if case == "cancel-io":
            await request.json()
            entered.set()
            await release.wait()
            return web.json_response({})
        return await fixture.handle(request)

    app.router.add_post("/v1/responses", handle)
    recorder = Recorder()
    supplied = set()
    root_id = "a" * 16
    trace_id = "1" * 32
    allocation_parents = []

    def generate():
        parent = telemetry.current_context()
        assert parent is not None
        allocation_parents.append(parent.span_id)
        # The automatic operation is created under the explicit root. The
        # provider scope is admitted under that operation, before provider I/O.
        if parent.span_id != root_id and case in {"invalid", "cancel"}:
            if case == "invalid":
                return "not-a-valid-span"
            raise asyncio.CancelledError("Synthetic ID admission cancellation")
        value = f"{0xFEED000000000000 + len(supplied) + 1:016x}"
        supplied.add(value)
        return value

    with TemporaryDirectory() as directory:
        async with (
            decoded_collector("http", "plain", Path(directory)) as (peer, _),
            TestServer(app) as model,
        ):
            await telemetry.configure(
                traces=telemetry.OtlpHttp(endpoint=f"{peer.endpoint}/v1/traces"),
                processors=[recorder],
            )
            telemetry.set_tracing_id_generator(
                telemetry.TracingIdGenerator(generate_span_id=generate)
            )
            try:
                async with NativeProvider(
                    base_url=str(model.make_url("/v1")),
                    api_key="synthetic-response-id",
                    supports_websockets=False,
                ) as provider:
                    with telemetry.trace(
                        "response-id.application",
                        options=telemetry.TraceOptions(trace_id=trace_id, span_id=root_id),
                    ):
                        agent = Agent(
                            model="gpt-5.5",
                            provider=provider,
                            instructions="Answer.",
                            tools=[echo] if case == "rounds" else [],
                        )
                        if case in {"rounds", "retry"}:
                            assert (await agent.run("Continue.")).final_output == "Complete."
                        elif case == "cancel-io":
                            pending = asyncio.create_task(agent.run("Wait for cancellation."))
                            try:
                                await asyncio.wait_for(entered.wait(), timeout=5)
                                pending.cancel()
                                with pytest.raises((asyncio.CancelledError, TurnStoppedError)):
                                    await pending
                            finally:
                                release.set()
                                if not pending.done():
                                    pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                        elif case == "invalid":
                            with pytest.raises(ValueError, match="Tracing ID generator"):
                                await agent.run("Never dispatch.")
                        else:
                            with pytest.raises((asyncio.CancelledError, TurnStoppedError)):
                                await agent.run("Never dispatch.")
                assert (await telemetry.force_flush()).status == "success"
                model_rows = [
                    row
                    for row in _spans(peer)
                    if row["name"] == "response" and row["traceId"] == trace_id
                ]
                print(
                    json.dumps(
                        {
                            "case": case,
                            "requests": len(fixture.requests),
                            "wire_attempts": len(attempts),
                            "response_span_ids": [row["spanId"] for row in model_rows],
                            "effects": effects,
                        }
                    )
                )
                if case in {"retry", "cancel-io"}:
                    assert len(attempts) == (2 if case == "retry" else 1)
                    assert len(fixture.requests) == (1 if case == "retry" else 0)
                    assert effects == []
                    assert len(model_rows) == 1
                    assert model_rows[0]["spanId"] in supplied
                    ends = [
                        event
                        for event in recorder.events
                        if event["trace_id"] == trace_id
                        and event["name"] == "response"
                        and event["lifecycle"] == "end"
                    ]
                    assert len(ends) == 1
                    assert ends[0]["span_id"] == model_rows[0]["spanId"]
                    assert ends[0]["attributes"]["sdk.outcome"] == (
                        "ok" if case == "retry" else "cancelled"
                    )
                    assert (
                        len(
                            [
                                event
                                for event in recorder.events
                                if event["trace_id"] == trace_id
                                and event["name"] == "response"
                                and event["lifecycle"] == "start"
                            ]
                        )
                        == 1
                    )
                    assert any(
                        row["name"] == "operation"
                        and _descends(model_rows[0], row["spanId"], _spans(peer))
                        for row in _spans(peer)
                    )
                elif case == "rounds":
                    assert len(model_rows) == len(fixture.requests) == 2
                    assert effects == ["echo"]
                    model_ids = {row["spanId"] for row in model_rows}
                    assert model_ids <= supplied
                    observed = {
                        row["span_id"]
                        for row in recorder.events
                        if row["name"] == "response" and row["trace_id"] == trace_id
                    }
                    assert observed == model_ids
                    operations = {
                        row["spanId"]
                        for row in _spans(peer)
                        if row["traceId"] == trace_id and row["name"] == "operation"
                    }
                    spans = _spans(peer)
                    assert all(
                        any(_descends(row, operation, spans) for operation in operations)
                        for row in model_rows
                    )
                    callback_rows = [row for row in spans if row["spanId"] in tool_span_ids]
                    assert len(callback_rows) == 1
                    assert not any(
                        _descends(callback, model["spanId"], spans)
                        for callback in callback_rows
                        for model in model_rows
                    )
                    diagnostics = [
                        row
                        for row in _spans(peer)
                        if row["name"] == "stream_request" and row["traceId"] == trace_id
                    ]
                    assert len(diagnostics) == 2
                    assert {row["spanId"] for row in diagnostics}.isdisjoint(supplied)
                    assert all(row["parentSpanId"] in model_ids for row in diagnostics)
                else:
                    assert fixture.requests == [] and effects == [] and model_rows == []
                    assert len(allocation_parents) >= 2
                print(f"PASS response span {case}")
            finally:
                telemetry.set_tracing_id_generator()
                await telemetry.shutdown()
