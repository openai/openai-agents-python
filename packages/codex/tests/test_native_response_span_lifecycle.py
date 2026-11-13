"""Real provider/Session acceptance for the proposed native response span.

Each case runs in a fresh process because the native tracing subscriber is global.
The accepted addon is an intentional red control; run again with a candidate addon.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ApprovalRequested,
    ModelRetrySettings,
    NativeProvider,
    PendingRequests,
    RetryBackoffSettings,
    function_tool,
    telemetry,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.telemetry_transport_fixtures import decoded_collector
from tests.test_tracing_native import Recorder, _attrs, _descends, _spans


@pytest.mark.parametrize(
    "case",
    [
        "complete",
        "approval",
        "retry",
        "retry-attempt-timeout",
        "cancel",
        "cancel-after-terminal",
        "disabled",
    ],
)
def test_native_response_span_lifecycle(case):
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    with TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio, sys; "
                "from tests.test_native_response_span_lifecycle import run; "
                "asyncio.run(asyncio.wait_for(run(sys.argv[1]), 30))",
                case,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def run(case):
    fixture_control = os.environ.get("TRACE_RESPONSE_SPAN_FIXTURE_CONTROL") == "1"
    fixture = ResponsesFixture()
    terminal_arrived, release_terminal = asyncio.Event(), asyncio.Event()
    effects = []
    retry_errors = []
    processor_changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    class LifecycleRecorder(Recorder):
        def on_trace_event(self, event):
            super().on_trace_event(event)
            loop.call_soon_threadsafe(processor_changed.set)

    recorder = LifecycleRecorder()
    supplied = set()
    trace_id = "12" * 16

    def next_span_id():
        value = f"{0xFEED000000000000 + len(supplied) + 1:016x}"
        supplied.add(value)
        return value

    @function_tool(needs_approval=True)
    def local_echo() -> str:
        """Record the approved effect."""
        with telemetry.span("response-span.tool-effect"):
            effects.append("approved")
        return "tool result"

    if case in {"approval", "cancel-after-terminal"}:
        fixture.respond(function_call("local_echo", {}, "response-span-call"))
    fixture.respond(assistant_output("Observed."))

    async def before_event(event):
        if event["type"] == "response.completed" and len(fixture.requests) == 1:
            terminal_arrived.set()
            if case in {"approval", "cancel", "cancel-after-terminal"}:
                await release_terminal.wait()

    fixture.before_event = before_event

    def retry_timeout(context):
        retry_errors.append(context.normalized.is_timeout)
        release_terminal.set()
        return context.normalized.is_timeout

    async def handle(request):
        if case == "retry-attempt-timeout" and not fixture.requests:
            fixture.requests.append(await request.json())
            fixture.request_headers.append(dict(request.headers))
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b": waiting for provider output\n\n")
            await release_terminal.wait()
            return response
        if case == "retry" and not fixture.requests:
            fixture.requests.append(await request.json())
            fixture.request_headers.append(dict(request.headers))
            # A finite SSE response without a terminal triggers Core's stream retry.
            created = {"type": "response.created", "response": {"id": "resp_abandoned"}}
            return web.Response(
                body=f"data: {json.dumps(created)}\n\n".encode(), content_type="text/event-stream"
            )
        return await fixture.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)

    def responses(lifecycle):
        return [
            e
            for e in recorder.events
            if e["trace_id"] == trace_id
            and e["name"] == "response"
            and e["lifecycle"] == lifecycle
            and e["attributes"].get("sdk.span.kind") == "model"
        ]

    async def wait_ends(count):
        try:
            async with timeout(5):
                while len(responses("end")) < count:
                    processor_changed.clear()
                    if len(responses("end")) < count:
                        await processor_changed.wait()
        except TimeoutError:
            raise AssertionError(
                "logical response did not end while approval remained pending"
            ) from None

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
                telemetry.TracingIdGenerator(
                    generate_trace_id=lambda: trace_id, generate_span_id=next_span_id
                )
            )
            try:
                provider = NativeProvider(
                    base_url=str(model.make_url("/v1")),
                    api_key="synthetic-response-span",
                    request_max_retries=0,
                    stream_max_retries=1 if case == "retry" else 0,
                )
                with telemetry.trace(
                    "response-span.application",
                    options=telemetry.TraceOptions(
                        disabled=case == "disabled", include_sensitive_data=True
                    ),
                ):
                    agent = Agent(
                        instructions="Answer.",
                        model="fixture",
                        provider=provider,
                        tools=[local_echo] if case in {"approval", "cancel-after-terminal"} else [],
                        model_settings=(
                            {
                                "attempt_timeout_ms": 1000,
                                "retry": ModelRetrySettings(
                                    max_retries=1,
                                    policy=retry_timeout,
                                    backoff=RetryBackoffSettings(
                                        initial_delay_ms=0, max_delay_ms=0, jitter=False
                                    ),
                                ),
                            }
                            if case == "retry-attempt-timeout"
                            else None
                        ),
                    )
                    async with agent.session() as session:
                        turn = await session.submit("Answer once.")
                        try:
                            if case in {"approval", "cancel", "cancel-after-terminal"}:
                                await terminal_arrived.wait()
                                # Streamed dispatch remains allowed before completion.
                                assert not responses("end"), (
                                    "response ended before provider terminal"
                                )
                                if case in {"approval", "cancel-after-terminal"}:
                                    boundary = await turn.wait_for_request_or_result()
                                    assert isinstance(boundary, PendingRequests)
                                    [request] = boundary.requests
                                    assert isinstance(request, ApprovalRequested)
                                    release_terminal.set()
                                    if not fixture_control:
                                        await wait_ends(1)
                                    assert not effects and len(fixture.requests) == 1
                                    if case == "approval":
                                        await session.respond(request.reply(True))
                                    else:
                                        await turn.cancel()
                                else:
                                    await turn.cancel()
                            if case not in {"cancel", "cancel-after-terminal"}:
                                assert (await turn.get_result()).final_output == "Observed."
                        finally:
                            release_terminal.set()
                retried = case in {"retry", "retry-attempt-timeout"}
                assert len(fixture.requests) == (2 if case == "approval" or retried else 1)
                assert retry_errors == ([True] if case == "retry-attempt-timeout" else [])
                assert effects == (["approved"] if case == "approval" else [])
                assert (await telemetry.force_flush()).status == "success"
                spans = [row for row in _spans(peer) if row["traceId"] == trace_id]
                if case == "disabled":
                    assert not spans and not [
                        e for e in recorder.events if e["trace_id"] == trace_id
                    ]
                    print(
                        json.dumps(
                            {"case": case, "requests": len(fixture.requests), "suppressed": True}
                        )
                    )
                    return
                logical = [
                    row
                    for row in spans
                    if row["name"] == "response" and _attrs(row).get("sdk.span.kind") == "model"
                ]
                diagnostic = [row for row in spans if row["name"] == "receiving_stream"]
                assert len(diagnostic) == (2 if case == "approval" or retried else 1)
                if fixture_control:
                    print(
                        json.dumps(
                            {
                                "case": case,
                                "fixture_control_only": True,
                                "requests": len(fixture.requests),
                                "effects": effects,
                                "attempts": len(diagnostic),
                            }
                        )
                    )
                    return
                expected_count = 2 if case == "approval" else 1
                assert len(logical) == expected_count, (
                    f"expected {expected_count} logical response spans; got {len(logical)}"
                )
                assert len(responses("start")) == len(responses("end")) == expected_count
                assert all(row["spanId"] in supplied for row in logical)
                assert len({row["spanId"] for row in logical}) == expected_count
                expected_ids = (
                    {"resp_1", "resp_2"}
                    if case == "approval"
                    else ({"resp_2"} if retried else {"resp_1"})
                )
                if case == "cancel":
                    assert not any("sdk.response.id" in _attrs(row) for row in logical)
                    assert responses("end")[0]["outcome"] == "error"
                    assert responses("end")[0]["attributes"]["sdk.outcome"] == "cancelled"
                else:
                    assert [event["outcome"] for event in responses("end")] == [
                        "ok"
                    ] * expected_count
                    assert {_attrs(row).get("sdk.response.id") for row in logical} == expected_ids
                assert all(row["spanId"] not in supplied for row in diagnostic)
                assert not any(
                    _descends(row, response["spanId"], spans)
                    for row in diagnostic
                    for response in logical
                )
                transport = [row for row in spans if row["name"] == "stream_request"]
                assert len(transport) == len(diagnostic)
                assert all(
                    sum(_descends(row, response["spanId"], spans) for response in logical) == 1
                    for row in transport
                )
                if retried:
                    assert all(_descends(row, logical[0]["spanId"], spans) for row in transport)
                if case == "approval":
                    [effect] = [row for row in spans if row["name"] == "response-span.tool-effect"]
                    assert not any(_descends(effect, row["spanId"], spans) for row in logical)
                    [first] = [
                        row for row in logical if _attrs(row).get("sdk.response.id") == "resp_1"
                    ]
                    assert int(first["endTimeUnixNano"]) <= int(effect["startTimeUnixNano"])
                print(
                    json.dumps(
                        {
                            "case": case,
                            "requests": len(fixture.requests),
                            "logical_ids": [s["spanId"] for s in logical],
                            "response_ids": [_attrs(s).get("sdk.response.id") for s in logical],
                            "attempt_ids": [s["spanId"] for s in transport],
                        }
                    )
                )
            finally:
                release_terminal.set()
                telemetry.set_tracing_id_generator()
                await telemetry.shutdown()
