"""REQ423–429 qualification through the real Python/native/collector boundaries.

Only the local model is scripted. Each case owns a fresh native subscriber;
processor, callback, queue and export behavior run without native mocks.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, field_serializer, model_validator

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    NativeProvider,
    OutputValidationError,
    ToolContext,
    TurnStoppedError,
    function_tool,
    telemetry,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.telemetry_transport_fixtures import (
    DecodedCollector,
    decoded_collector,
    require_qualification_dependencies,
)

require_qualification_dependencies()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class Recorder:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def on_trace_event(self, event: Mapping[str, Any]) -> None:
        snapshot = _plain(event)
        # Native lifecycle snapshots must themselves survive JSON serialization.
        json.dumps(snapshot, allow_nan=False)
        with self._lock:
            self._events.append(snapshot)

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)


def _hex(value: str) -> str:
    # The official protobuf JSON formatter represents bytes as base64.
    return base64.b64decode(value, validate=True).hex()


def _spans(peer: DecodedCollector) -> list[dict[str, Any]]:
    return [
        {
            **span,
            "traceId": _hex(span["traceId"]),
            "spanId": _hex(span["spanId"]),
            "parentSpanId": _hex(span["parentSpanId"]) if span.get("parentSpanId") else None,
        }
        for record in peer.records
        if record["signal"] == "traces"
        for resource in record["payload"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]


def _attrs(span: Mapping[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span.get("attributes", [])}


def _descends(span: Mapping[str, Any], ancestor: str, spans: list[dict[str, Any]]) -> bool:
    parents = {row["spanId"]: row.get("parentSpanId") for row in spans}
    current = span.get("parentSpanId")
    seen = set()
    while current and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _answer(fixture: ResponsesFixture, text: str) -> None:
    response = assistant_output(text)
    response["item"]["phase"] = "final_answer"
    fixture.respond(response)


@pytest.mark.parametrize(
    "case",
    [
        "ancestry_queue",
        "policies",
        "outcomes",
        "credentials_http",
        "credentials_grpc",
        "processors_flush",
        "processor_failures",
        "bounds",
        "no_export",
        "flush_budget",
        "manual_restore",
        "shutdown_budget",
        "mcp_policy",
        "terminal_queue",
        "host_contexts",
        "manual_validation_context",
        "prepared_request_context",
        "closed_session_context",
        "child_turn_policy",
        "child_followup_trace",
        "child_followup_reloaded_trace",
        "child_interaction_policy",
    ],
)
def test_native_tracing_contract(case: str) -> None:
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    with tempfile.TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio, sys; "
                "from tests.test_tracing_native import _native_case; "
                "asyncio.run(asyncio.wait_for(_native_case(sys.argv[1]), 50))",
                case,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def _native_case(case: str) -> None:
    if case == "terminal_queue":
        await _shutdown_seals_queued_exports()
        return
    fixture = ResponsesFixture()
    application = web.Application()
    application.router.add_post("/v1/responses", fixture.handle)
    transport = "grpc" if case == "credentials_grpc" else "http"
    recorder = Recorder()
    with tempfile.TemporaryDirectory() as directory:
        async with (
            decoded_collector(transport, "plain", Path(directory)) as (peer, _),
            TestServer(application) as model,
        ):
            export = (
                telemetry.OtlpGrpc(endpoint=peer.endpoint, headers={"authorization": "default-key"})
                if transport == "grpc"
                else telemetry.OtlpHttp(
                    endpoint=f"{peer.endpoint}/v1/traces", headers={"authorization": "default-key"}
                )
            )
            if case == "no_export":
                await telemetry.configure()
            else:
                await telemetry.configure(
                    traces=export,
                    logs=telemetry.OtlpHttp(endpoint=f"{peer.endpoint}/v1/logs")
                    if case
                    in (
                        "policies",
                        "ancestry_queue",
                        "child_turn_policy",
                        "child_followup_trace",
                        "child_followup_reloaded_trace",
                    )
                    else None,
                    processors=[recorder],
                    log_user_prompt=True,
                    log_agent_responses=True,
                    tool_result_max_bytes=4096,
                )
            provider = NativeProvider(base_url=str(model.make_url("/v1")), api_key="model-only-key")
            try:
                if case == "ancestry_queue":
                    await _ancestry_queue(fixture, provider, peer, recorder)
                elif case == "policies":
                    await _policies(fixture, provider, peer, recorder)
                elif case == "outcomes":
                    await _outcomes(fixture, provider, recorder)
                elif case.startswith("credentials_"):
                    await _credentials(fixture, provider, peer, recorder)
                elif case == "processors_flush":
                    await _processors_flush(fixture, provider, peer, recorder)
                elif case == "processor_failures":
                    await _processor_failures(fixture, provider)
                elif case == "bounds":
                    await _bounds(fixture, provider, recorder)
                elif case == "no_export":
                    await _no_export(fixture, provider, peer, recorder)
                elif case == "flush_budget":
                    await _flush_budget()
                elif case == "shutdown_budget":
                    await _flush_budget(shutdown=True)
                elif case == "manual_restore":
                    await _manual_restore(fixture, provider, peer)
                elif case == "mcp_policy":
                    await _mcp_policy(fixture, provider, recorder)
                elif case == "host_contexts":
                    await _host_contexts(recorder)
                elif case == "manual_validation_context":
                    await _manual_validation_context(fixture, provider)
                elif case == "prepared_request_context":
                    await _prepared_request_context(fixture, str(model.make_url("/v1")))
                elif case == "closed_session_context":
                    await _closed_session_context(fixture, provider)
                elif case == "child_turn_policy":
                    await _child_turn_policy(recorder, peer, Path(directory))
                elif case == "child_followup_trace":
                    await _child_followup_trace(peer, Path(directory))
                elif case == "child_followup_reloaded_trace":
                    await _child_followup_reloaded_trace(peer, Path(directory))
                elif case == "child_interaction_policy":
                    await _child_interaction_policy(Path(directory))
                else:
                    raise AssertionError(case)
            finally:
                await provider.aclose()
                await telemetry.shutdown(timeout_ms=5000)


async def _prepared_request_context(fixture: ResponsesFixture, url: str) -> None:
    provider = NativeProvider(base_url=url, api_key="model-only-key", supports_websockets=False)
    try:
        for disabled in (False, True):
            observed = []

            def edit(turn, request, observed=observed, **indices):
                with telemetry.span(
                    "inside request editor",
                    options=telemetry.TraceOptions(include_sensitive_data=True),
                ) as child:
                    observed.append(
                        (
                            child.context.trace_id,
                            child.context.disabled,
                            child.context.include_sensitive_data,
                        )
                    )
                request["instructions"] = "edited instructions"
                return request

            _answer(fixture, "done")
            agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
            async with agent.session() as session:
                with telemetry.trace(
                    "submitted workflow",
                    options=telemetry.TraceOptions(disabled=disabled, include_sensitive_data=False),
                ) as workflow:
                    turn = await session.submit("answer", prepare_request=edit)
                assert telemetry.current_context() is None
                assert (await turn.get_result()).final_output == "done"
            assert observed == [(workflow.context.trace_id, disabled, False)]
            assert "edited instructions" in json.dumps(fixture.requests[-1])
        assert len(fixture.requests) == 2
    finally:
        await provider.aclose()


async def _closed_session_context(fixture: ResponsesFixture, provider: NativeProvider) -> None:
    import weakref

    _answer(fixture, "done")
    with telemetry.trace("opening") as opening:
        opening_ref = weakref.ref(opening.context)
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            with telemetry.trace(
                "turn caller", options=telemetry.TraceOptions(detached=True)
            ) as caller:
                caller_ref = weakref.ref(caller.context)
                turn = await session.submit("one")
                assert (await turn.get_result()).final_output == "done"
    del opening, caller
    await asyncio.sleep(0)
    gc.collect()
    assert session._closed and turn is not None
    assert opening_ref() is None and caller_ref() is None


async def _manual_validation_context(fixture: ResponsesFixture, provider: NativeProvider) -> None:
    for mode in ("metadata", "disabled", "unscoped"):
        trace_id = "a" * 32 if mode != "unscoped" else None
        seen: list[str] = []

        def observe(
            stage: str, trace_id: str | None = trace_id, mode: str = mode, seen: list[str] = seen
        ) -> None:
            context = telemetry.current_context()
            if trace_id is None:
                assert context is None
            else:
                assert context is not None and context.trace_id == trace_id
                assert context.disabled == (mode == "disabled")
                assert not context.include_sensitive_data
            with telemetry.span(
                stage, options=telemetry.TraceOptions(include_sensitive_data=True)
            ) as child:
                if trace_id is not None:
                    assert child.context.trace_id == trace_id
                    assert child.context.disabled == (mode == "disabled")
                    assert not child.context.include_sensitive_data
            seen.append(stage)

        class Input(BaseModel):
            key: str

            @model_validator(mode="after")
            def validate_input(self) -> Any:
                observe("input")
                return self

        class Output(BaseModel):
            answer: str

            @model_validator(mode="after")
            def validate_output(self) -> Any:
                observe("output")
                return self

            @field_serializer("answer")
            def serialize_answer(self, value: str) -> str:
                observe("serializer")
                return value

        manual = FunctionTool(
            name="manual", description="Read a key.", input_type=Input, output_type=Output
        )
        fixture.respond(function_call("manual", {"key": "synthetic"}, f"manual-{mode}"))
        _answer(fixture, "done")
        options = (
            None
            if trace_id is None
            else telemetry.TraceOptions(
                trace_id=trace_id, disabled=mode == "disabled", include_sensitive_data=False
            )
        )
        async with Agent(instructions="Use manual.", tools=[manual], provider=provider).session(
            trace_options=options
        ) as session:
            turn = await session.submit("read")
            with telemetry.trace(
                "unrelated consumer", options=telemetry.TraceOptions(detached=True)
            ) as consumer:
                async for event in session.interactions():
                    request = manual.match(event)
                    if request is not None:
                        await request.respond({"answer": "accepted"})
                        break
                assert telemetry.current_context() is consumer.context
            assert (await turn.get_result()).final_output == "done"
        assert seen == ["input", "output", "serializer"]


async def _host_contexts(recorder: Recorder) -> None:
    from agents import _native

    release = asyncio.Event()
    children: list[tuple[str, bool]] = []

    async def delayed() -> None:
        await release.wait()
        with telemetry.span(
            "late task", options=telemetry.TraceOptions(include_sensitive_data=True)
        ) as child:
            children.append((child.context.trace_id, child.context.include_sensitive_data))

    with telemetry.trace("closed parent") as owner:
        parent = owner.context
        task = asyncio.create_task(delayed())
    release.set()
    await task
    assert children == [(parent.trace_id, False)]
    with telemetry.span(
        "explicit late parent", options=telemetry.TraceOptions(parent=parent)
    ) as child:
        assert child.context.parent_span_id == parent.span_id
    token = parent._handle
    del owner, parent, task, child
    await asyncio.sleep(0)
    gc.collect()
    with pytest.raises(RuntimeError, match="Unknown or released"):
        _native.trace_span_context(token)
    assert (await telemetry.force_flush()).status == "success"
    seen: list[str] = []

    class Instrumented:
        async def on_trace_event(self, event: Mapping[str, Any]) -> None:
            await asyncio.sleep(0)
            seen.append(event["name"])
            with telemetry.trace(
                "processor work", options=telemetry.TraceOptions(disabled=False, detached=True)
            ) as scope:
                assert scope.context.disabled

    telemetry.add_trace_processor(Instrumented())
    with telemetry.trace("recursion seed"):
        pass
    flushed = await telemetry.force_flush(timeout_ms=2000)
    assert flushed.status == "success" and not flushed.failed_deliveries, flushed
    assert seen == ["recursion seed", "recursion seed"]
    assert not any(event["name"] == "processor work" for event in recorder.events)


async def _shutdown_seals_queued_exports() -> None:
    entered, release, unexpected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    batches: list[int] = []

    async def receive(request: web.Request) -> web.Response:
        payload = await request.json()
        batches.append(
            sum(
                len(scope.get("spans", []))
                for resource in payload.get("resourceSpans", [])
                for scope in resource.get("scopeSpans", [])
            )
        )
        if len(batches) == 1:
            entered.set()
            await release.wait()
        else:
            unexpected.set()
        return web.json_response({})

    application = web.Application()
    application.router.add_post("/v1/traces", receive)
    async with TestServer(application) as collector:
        await telemetry.configure(
            traces=telemetry.OtlpHttp(
                endpoint=str(collector.make_url("/v1/traces")), protocol="json"
            )
        )
        try:
            for index in range(6):
                credentials = telemetry.TraceExportCredentials(
                    headers={"authorization": f"synthetic-private-{index}"}
                )
                with telemetry.trace(
                    f"queued-{index}",
                    options=telemetry.TraceOptions(export_credentials=credentials),
                ):
                    pass
            await asyncio.wait_for(entered.wait(), 10)
            try:
                await asyncio.wait_for(telemetry.shutdown(timeout_ms=30), 2)
            except RuntimeError as error:
                message = str(error).lower()
                assert "time budget" in message or message.startswith(
                    "telemetry shutdown timed out waiting for"
                )
            assert batches == [1]
            release.set()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(unexpected.wait(), 0.25)
            assert batches == [1]
        finally:
            release.set()


async def _mcp_policy(
    fixture: ResponsesFixture, provider: NativeProvider, recorder: Recorder
) -> None:
    from tests.test_native_mcp_tools import Peer, mcp_call

    seen: list[tuple[telemetry.SpanContext, telemetry.SpanContext]] = []

    async def transform(context: Any, value: dict[str, Any]) -> dict[str, Any]:
        current = telemetry.current_context()
        assert current is not None
        with telemetry.span(
            "MCP policy child", options=telemetry.TraceOptions(include_sensitive_data=True)
        ) as child:
            seen.append((current, child.context))
        return value

    for disabled in (False, True):
        expected_id = ("b" if disabled else "a") * 32
        fixture.respond(mcp_call("search", "mcp__docs", "mcp-context-call"))
        _answer(fixture, "done")
        async with Peer(names=("search",)).serve() as url:
            agent = Agent(
                instructions="Read the record.",
                provider=provider,
                tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=transform)],
            )
            assert telemetry.current_context() is None
            async with agent.session(
                trace_options=telemetry.TraceOptions(
                    trace_id=expected_id, disabled=disabled, include_sensitive_data=False
                )
            ) as session:
                assert (await (await session.submit("read")).get_result()).final_output == "done"
                assert "tracing_context" not in (await session.save_state()).to_json()
        current, child = seen[-1]
        assert current.trace_id == child.trace_id == expected_id
        assert current.disabled == child.disabled == disabled
        assert not current.include_sensitive_data and not child.include_sensitive_data
        assert child.parent_span_id == current.span_id
        assert telemetry.current_context() is None
    assert len(seen) == 2
    flushed = await telemetry.force_flush(timeout_ms=2000)
    assert flushed.status == "success", flushed
    ended = [event for event in recorder.events if event["lifecycle"] == "end"]
    callbacks = [event for event in ended if event["name"] == "MCP callback"]
    children = [event for event in ended if event["name"] == "MCP policy child"]
    assert len(callbacks) == len(children) == 1
    assert callbacks[0]["trace_id"] == "a" * 32
    active_callbacks = [event for event in ended if event["span_id"] == seen[0][0].span_id]
    assert len(active_callbacks) == 1 and active_callbacks[0]["name"] == "callback"
    assert active_callbacks[0]["parent_span_id"] == callbacks[0]["span_id"]
    assert children[0]["parent_span_id"] == active_callbacks[0]["span_id"]
    assert not any(event["trace_id"] == "b" * 32 for event in recorder.events)


async def _flush_budget(*, shutdown: bool = False) -> None:
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            entered.set()
            release.wait(10)

    telemetry.add_trace_processor(Blocked())
    with telemetry.trace("flush deadline regression"):
        pass
    assert await asyncio.to_thread(entered.wait, 2)
    first = asyncio.create_task(telemetry.force_flush(timeout_ms=500))
    try:
        # Let the first call enter native and wait for the blocked processor.
        await asyncio.sleep(0.03)
        assert not first.done()
        started = time.monotonic()
        shutdown_error = None
        if shutdown:
            try:
                await telemetry.shutdown(timeout_ms=20)
            except RuntimeError as error:
                shutdown_error = error
        else:
            second = await telemetry.force_flush(timeout_ms=20)
            assert second.status == "timeout", second
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, f"20 ms operation waited {elapsed:.3f}s behind another flush"
        if shutdown:
            assert shutdown_error is not None, "Shutdown must report owner-lock admission timeout"
            assert (
                "timeout" in str(shutdown_error).lower()
                or "timed out" in str(shutdown_error).lower()
            )
            # Shutdown retires the pump even when owner admission times out.
            # Its current batch is acknowledged as failed. Later queued events
            # may leave the first flush waiting until its own deadline.
            result = await first
            assert result.status in ("partial", "timeout"), result
            assert result.failed_deliveries > 0, result
        else:
            assert not first.done()
    finally:
        release.set()
        await first


async def _manual_restore(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector
) -> None:
    checking_restore = False
    restored_input_contexts = []

    class Arguments(BaseModel):
        key: str

        @model_validator(mode="after")
        def check_restored_context(self) -> Arguments:
            if checking_restore:
                current = telemetry.current_context()
                assert current is not None
                assert current.trace_id == restored_scope.context.trace_id
                assert not current.include_sensitive_data
                with telemetry.span(
                    "restored validator nested work",
                    options=telemetry.TraceOptions(include_sensitive_data=True),
                ) as child:
                    assert not child.context.include_sensitive_data
                restored_input_contexts.append(current)
            return self

    manual = FunctionTool(
        name="manual_lookup", description="Read a record.", input_type=Arguments, output_type=str
    )
    agent = Agent(instructions="Answer.", model="gpt-5.1-codex", tools=[manual], provider=provider)
    fixture.respond(function_call("manual_lookup", {"key": "synthetic"}, "restored-call"))
    with telemetry.trace("source workflow") as source_scope:
        async with agent.session() as source:
            original = await source.submit("pause")
            async for interaction in source.interactions():
                if manual.match(interaction) is not None:
                    break
            else:
                raise AssertionError("Source manual request was not delivered")
            original_turn_id = original.turn_id
            saved = await source.suspend()
    assert saved is not None
    serialized = saved.to_json()

    # Native history can retain historical trace IDs. It must not retain a
    # live scope lease or the process's exporter credentials/routing context.
    # Registry handles are short tokens, so exclude the complete JSON value
    # rather than coincidental substrings of retained revision/content hashes.
    assert json.dumps(source_scope.context._handle) not in serialized
    assert "tracing_context" not in serialized
    assert "credential_ref" not in serialized
    assert "default-key" not in serialized
    _answer(fixture, "RESTORED_OUTPUT_CANARY")
    checking_restore = True
    with telemetry.trace(
        "restored workflow",
        options=telemetry.TraceOptions(
            include_sensitive_data=True,
        ),
    ) as restored_scope:
        async with agent.session(
            state=saved, trace_options=telemetry.TraceOptions(include_sensitive_data=False)
        ) as restored:
            assert len(restored_input_contexts) == 1
            async for interaction in restored.interactions():
                request = manual.match(interaction)
                if request is not None:
                    break
            else:
                raise AssertionError("Restored manual request was not delivered")
            assert request.turn_id == original_turn_id
            assert len(fixture.requests) == 1
            await request.respond("restored result")
            turn = restored.restored_turn
            assert turn is not None
            assert (await turn.get_result()).final_output == "RESTORED_OUTPUT_CANARY"
    result = await telemetry.force_flush()
    assert result.status == "success", result
    spans = _spans(peer)
    continuation = [row for row in spans if row["traceId"] == restored_scope.context.trace_id]
    assert any(row["name"] == "receiving_stream" for row in continuation)
    assert any(_attrs(row).get("sdk.span.kind") == "validation" for row in continuation)
    assert all(
        _descends(row, restored_scope.context.span_id, spans)
        for row in continuation
        if row["spanId"] != restored_scope.context.span_id
    )
    assert "RESTORED_OUTPUT_CANARY" not in json.dumps(continuation)
    assert len(fixture.requests) == 2


async def _no_export(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector, recorder: Recorder
) -> None:
    contexts: list[telemetry.SpanContext] = []

    @function_tool
    def lookup(key: str) -> str:
        with telemetry.span("unexported callback") as callback:
            assert callback.context.trace_id == root.context.trace_id
            assert not callback.context.include_sensitive_data
            contexts.append(callback.context)
        return key

    fixture.respond(function_call("lookup", {"key": "synthetic"}, "no-export-call"))
    _answer(fixture, "answer")
    with telemetry.trace("unexported workflow") as root:
        with telemetry.span(
            "unexported child",
            options=telemetry.TraceOptions(
                include_sensitive_data=True,
            ),
        ) as child:
            assert child.context.trace_id == root.context.trace_id
            assert child.context.parent_span_id == root.context.span_id
            assert not child.context.include_sensitive_data
            contexts.extend((root.context, child.context))
            completed = await Agent(
                instructions="Answer.", model="gpt-5.1-codex", tools=[lookup], provider=provider
            ).run("one")
            assert completed.final_output == "answer"
    with telemetry.trace("disabled without export", options=telemetry.TraceOptions(disabled=True)):
        with telemetry.span(
            "cannot relax",
            options=telemetry.TraceOptions(
                disabled=False,
                include_sensitive_data=True,
            ),
        ) as restricted:
            assert restricted.context.disabled
            assert not restricted.context.include_sensitive_data
            contexts.append(restricted.context)
    assert len(contexts) == 4
    assert all(
        len(context.trace_id) == 32 and int(context.trace_id, 16) != 0 for context in contexts
    )
    assert all(len(context.span_id) == 16 and int(context.span_id, 16) != 0 for context in contexts)
    with pytest.raises(RuntimeError, match="Configure a trace exporter or trace processors"):
        telemetry.add_trace_processor(recorder)
    result = await telemetry.force_flush(timeout_ms=1000)
    assert result.status == "success", result
    assert not peer.records
    assert not recorder.events
    assert len(fixture.requests) == 2


async def _ancestry_queue(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector, recorder: Recorder
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        entered.set()
        await release.wait()
        with telemetry.span("inside callback"):
            context = telemetry.current_context()
            assert context is not None
            effects.append(context.trace_id)
        return key

    fixture.respond(function_call("lookup", {"key": "one"}, "lookup-one"))
    _answer(fixture, "first")
    _answer(fixture, "queued")
    agent = Agent(instructions="Answer.", model="gpt-5.1-codex", tools=[lookup], provider=provider)
    async with agent.session() as session:
        with telemetry.trace(
            "first workflow",
            options=telemetry.TraceOptions(
                trace_id="trace_123456789012345678901234567890ab",
                group_id="group-one",
                metadata={"tenant": "synthetic-one"},
            ),
        ) as first_scope:
            with telemetry.span("custom parent") as custom:
                first = await session.submit("one", trace_options=telemetry.TraceOptions())
        await entered.wait()
        with telemetry.trace("queued workflow") as queued_scope:
            queued = await session.submit(
                "two", if_busy="queue", trace_options=telemetry.TraceOptions()
            )
        # Both caller scopes have ended before the queued work enters native.
        release.set()
        assert (await first.get_result()).final_output == "first"
        assert (await queued.get_result()).final_output == "queued"
    result = await telemetry.force_flush()
    assert result.status == "success", result
    assert effects == [first_scope.context.trace_id]
    spans = _spans(peer)
    assert first_scope.context.trace_id == "123456789012345678901234567890ab"
    root = next(row for row in spans if row["spanId"] == first_scope.context.span_id)
    assert _attrs(root)["sdk.group_id"] == "group-one"
    assert _attrs(root)["sdk.metadata.tenant"] == "synthetic-one"
    custom_span = next(row for row in spans if row["spanId"] == custom.context.span_id)
    assert custom_span["parentSpanId"] == root["spanId"]
    native = [
        row
        for row in spans
        if row["traceId"] == root["traceId"] and "sdk.span.kind" not in _attrs(row)
    ]
    assert any(
        row["name"] == "receiving_stream" and _descends(row, custom.context.span_id, spans)
        for row in native
    )
    assert any(
        _attrs(row).get("tool_name") == "lookup" and _descends(row, custom.context.span_id, spans)
        for row in native
    )
    assert any(
        _attrs(row).get("sdk.span.kind") == "callback"
        and _descends(row, custom.context.span_id, spans)
        for row in spans
    )
    # Metadata-only workflows retain trace-safe span events; diagnostic logs
    # containing tool arguments are correctly suppressed by that policy.
    tool_records = [
        event
        for row in native
        for event in row.get("events", [])
        if _attrs(event).get("event.name") in ("codex.tool_call_received", "codex.tool_result")
    ]
    assert tool_records
    assert any(
        row["name"] == "inside callback" and _descends(row, custom.context.span_id, spans)
        for row in spans
    )
    queued_spans = [row for row in spans if row["traceId"] == queued_scope.context.trace_id]
    assert any(row["name"] == "receiving_stream" for row in queued_spans)
    events = recorder.events
    assert any(
        event["lifecycle"] == "end" and event["trace_id"] == queued_scope.context.trace_id
        for event in events
    )


async def _policies(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector, recorder: Recorder
) -> None:
    @function_tool
    def lookup(key: str) -> str:
        return "TOOL_OUTPUT_CANARY_6812:" + key

    agent = Agent(instructions="Answer.", model="gpt-5.1-codex", tools=[lookup], provider=provider)
    identities: dict[str, str] = {}
    for name in ("disabled", "metadata", "payload"):
        fixture.respond(function_call("lookup", {"key": "TOOL_ARGUMENT_CANARY_5113"}, name))
        _answer(fixture, "MODEL_OUTPUT_CANARY_3357")
        with telemetry.trace(
            name,
            options=telemetry.TraceOptions(
                disabled=name == "disabled",
                include_sensitive_data=name == "payload",
            ),
        ) as scope:
            identities[name] = scope.context.trace_id
            with telemetry.span(
                "child cannot relax",
                options=telemetry.TraceOptions(
                    disabled=False,
                    include_sensitive_data=True,
                    detached=name == "disabled",
                ),
            ) as child:
                if name == "disabled":
                    identities["disabled_child"] = child.context.trace_id
                completed = await agent.run("PROMPT_CANARY_8543")
                assert completed.final_output == "MODEL_OUTPUT_CANARY_3357"
    result = await telemetry.force_flush()
    assert result.status == "success", result
    spans = _spans(peer)
    suppressed = {identities["disabled"], identities["disabled_child"]}
    assert all(row["traceId"] not in suppressed for row in spans)
    assert all(event["trace_id"] not in suppressed for event in recorder.events)
    metadata_records = [
        row
        for record in peer.records
        for row in _record_rows(record)
        if row["traceId"] == identities["metadata"]
    ]
    payload_records = [
        row
        for record in peer.records
        for row in _record_rows(record)
        if row["traceId"] == identities["payload"]
    ]
    metadata = json.dumps(metadata_records)
    payload = json.dumps(payload_records)
    for canary in (
        "TOOL_ARGUMENT_CANARY_5113",
        "TOOL_OUTPUT_CANARY_6812",
        "MODEL_OUTPUT_CANARY_3357",
        "PROMPT_CANARY_8543",
    ):
        assert canary not in metadata
        assert canary in payload
    assert any(row["traceId"] == identities["metadata"] for row in spans)


def _record_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    if record["signal"] == "traces":
        return [
            {**span, "traceId": _hex(span["traceId"])}
            for resource in record["payload"]["resourceSpans"]
            for scope in resource["scopeSpans"]
            for span in scope["spans"]
        ]
    return [
        {**row, "traceId": _hex(row["traceId"]) if row.get("traceId") else None}
        for resource in record["payload"]["resourceLogs"]
        for scope in resource["scopeLogs"]
        for row in scope["logRecords"]
    ]


async def _outcomes(
    fixture: ResponsesFixture, provider: NativeProvider, recorder: Recorder
) -> None:
    @function_tool
    def broken() -> str:
        raise ValueError("PRIVATE_CALLBACK_CANARY")

    fixture.respond(function_call("broken", {}, "broken-one"))
    with telemetry.trace("callback failure"):
        with pytest.raises(ValueError, match="PRIVATE_CALLBACK_CANARY"):
            await Agent(
                instructions="Act.", model="gpt-5.1-codex", tools=[broken], provider=provider
            ).run("fail")

    class Answer(BaseModel):
        answer: int

    _answer(fixture, '{"answer":"not-an-int"}')
    with telemetry.trace("validation failure"):
        with pytest.raises(OutputValidationError):
            await Agent(
                instructions="Act.", model="gpt-5.1-codex", provider=provider, output_type=Answer
            ).run("invalid")
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate(event: dict[str, Any]) -> None:
        entered.set()
        await release.wait()

    fixture.before_event = gate
    _answer(fixture, "must not complete")
    try:
        async with Agent(
            instructions="Act.", model="gpt-5.1-codex", provider=provider
        ).session() as session:
            with telemetry.trace("cancelled operation"):
                turn = await session.submit("cancel", trace_options=telemetry.TraceOptions())
            await entered.wait()
            await turn.cancel()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
    finally:
        release.set()
        fixture.before_event = None
    result = await telemetry.force_flush()
    assert result.status == "success", result
    ends = [event for event in recorder.events if event["lifecycle"] == "end"]
    categories = {event["attributes"].get("sdk.outcome.category") for event in ends}
    assert {"callback_error", "validation_error", "cancelled"} <= categories
    assert "PRIVATE_CALLBACK_CANARY" not in json.dumps(ends)
    assert all(
        event["outcome"] == "error"
        for event in ends
        if event["attributes"].get("sdk.outcome.category")
        in {"callback_error", "validation_error", "cancelled"}
    )


async def _credentials(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector, recorder: Recorder
) -> None:
    identities = {}
    secrets = {"alpha": "synthetic-alpha-private", "beta": "synthetic-beta-private"}
    for name, secret in secrets.items():
        _answer(fixture, name)
        with telemetry.trace(
            name,
            options=telemetry.TraceOptions(
                export_credentials=telemetry.TraceExportCredentials(
                    headers={"authorization": secret}
                )
            ),
        ) as scope:
            identities[name] = scope.context.trace_id
            async with Agent(
                instructions="Answer.", model="gpt-5.1-codex", provider=provider
            ).session() as session:
                assert (await (await session.submit(name)).get_result()).final_output == name
                saved = (await session.save_state()).to_json()
                assert all(value not in saved for value in secrets.values())
    result = await telemetry.force_flush()
    assert result.status == "success", result
    for name, identity in identities.items():
        matching = [
            record
            for record in peer.records
            if any(row["traceId"] == identity for row in _record_rows(record))
        ]
        assert matching, name
        assert all(record["headers"].get("authorization") == secrets[name] for record in matching)
    serialized = json.dumps([record["payload"] for record in peer.records]) + json.dumps(
        recorder.events
    )
    assert all(secret not in serialized for secret in secrets.values())
    assert all(
        request.get("authorization") == "Bearer model-only-key"
        for request in fixture.request_headers
    )


async def _processors_flush(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector, recorder: Recorder
) -> None:
    additional, replacement = Recorder(), Recorder()
    telemetry.add_trace_processor(additional)
    agent = Agent(instructions="Answer.", model="gpt-5.1-codex", provider=provider)
    async with agent.session() as session:
        _answer(fixture, "first")
        with telemetry.trace("add route"):
            assert (await (await session.submit("one")).get_result()).final_output == "first"
        result = await telemetry.force_flush(timeout_ms=5000)
        assert result.status == "success", result
        first = [event for event in recorder.events if event["name"] == "add route"]
        second = [event for event in additional.events if event["name"] == "add route"]
        assert [event["lifecycle"] for event in first] == ["start", "end"]
        assert [event["sequence"] for event in first] == [event["sequence"] for event in second]
        assert any(row["name"] == "add route" for row in _spans(peer))
        telemetry.set_trace_processors([replacement])
        _answer(fixture, "second")
        with telemetry.trace("replacement route"):
            assert (await (await session.submit("two")).get_result()).final_output == "second"
        result = await telemetry.force_flush(timeout_ms=5000)
        assert result.status == "success", result
        assert any(event["name"] == "replacement route" for event in replacement.events)
        assert not any(
            event["name"] == "replacement route" for event in recorder.events + additional.events
        )
        assert not any(row["name"] == "replacement route" for row in _spans(peer))


async def _processor_failures(fixture: ResponsesFixture, provider: NativeProvider) -> None:
    release = threading.Event()

    class Broken:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            raise RuntimeError("PRIVATE_PROCESSOR_CANARY")

    class Stalled:
        def on_trace_event(self, event: Mapping[str, Any]) -> None:
            release.wait(30)

    telemetry.add_trace_processor(Broken())
    telemetry.add_trace_processor(Stalled())
    effects: list[str] = []

    @function_tool
    def effect(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(instructions="Act.", model="gpt-5.1-codex", provider=provider, tools=[effect])
    try:
        for name in ("one", "two"):
            fixture.respond(function_call("effect", {"value": name}, name))
            _answer(fixture, name)
            with telemetry.trace("processor failure " + name):
                assert (await agent.run(name)).final_output == name
            started = time.monotonic()
            result = await telemetry.force_flush(timeout_ms=1500)
            assert time.monotonic() - started < 3
            assert result.status in ("partial", "timeout", "failure"), result
        assert effects == ["one", "two"]
    finally:
        release.set()
        telemetry.set_trace_processors([])


async def _bounds(fixture: ResponsesFixture, provider: NativeProvider, recorder: Recorder) -> None:
    for metadata in ({"huge": "é" * 2049}, {"too_many": list(range(65))}):
        with pytest.raises((ValueError, RuntimeError), match="Tracing|tracing"):
            with telemetry.trace(
                "invalid metadata", options=telemetry.TraceOptions(metadata=metadata)
            ):
                raise AssertionError("Invalid metadata was admitted")
    assert not fixture.requests
    _answer(fixture, "answer")
    with telemetry.trace("usage"):
        await Agent(instructions="Answer.", model="gpt-5.1-codex", provider=provider).run("one")
    result = await telemetry.force_flush()
    assert result.status == "success", result
    events = recorder.events
    assert events
    assert all(len(json.dumps(event).encode()) <= 65536 for event in events)
    assert all(event["start_time_unix_nanos"].isdigit() for event in events)
    usage = [
        event["attributes"]
        for event in events
        if event["lifecycle"] == "end" and "gen_ai.usage.input_tokens" in event["attributes"]
    ]
    assert usage
    assert any(
        row["gen_ai.usage.input_tokens"] == 11
        and row["gen_ai.usage.output_tokens"] == 3
        and row["codex.usage.total_tokens"] == 14
        for row in usage
    )


@pytest.mark.parametrize("new_events", [False, True])
def test_processor_loss_survives_sequential_event_loops(new_events: bool) -> None:
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    script = r"""
import asyncio,json,threading,sys
from agents import telemetry
from _agents_codex import telemetry as runtime_telemetry
entered=threading.Event(); release=threading.Event(); seen=[]
class Processor:
    def on_trace_event(self,event):
        seen.append((event['sequence'],event['name'],event['lifecycle']))
        if len(seen)==1:
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic callback was not released')
async def first():
    await telemetry.configure(processors=[Processor()])
    with telemetry.trace('first'): pass
    assert await asyncio.to_thread(entered.wait,2)
async def second():
    if sys.argv[1]=='True':
        with telemetry.trace('second'): pass
    result=await telemetry.force_flush(timeout_ms=2000)
    assert result.status=='partial', result
    assert result.dropped_records==0, result
    first_events=[lifecycle for _,name,lifecycle in seen if name=='first']
    assert first_events in (['start'], ['start','end']), first_events
    # The pump may select start before end is queued. Only the selected
    # records are lost at loop shutdown; a queued end can reach the next loop.
    assert result.failed_deliveries==2-int('end' in first_events), result
    again=await telemetry.force_flush(timeout_ms=2000)
    assert again.status=='partial', again
    assert again.dropped_records==0, again
    assert again.failed_deliveries==result.failed_deliveries, again
    assert [lifecycle for _,name,lifecycle in seen if name=='first']==first_events
    if sys.argv[1]=='True':
        second_events=[(name,lifecycle) for _,name,lifecycle in seen if name=='second']
        assert second_events==[('second','start'),('second','end')]
    print(json.dumps({'seen':seen,'flush':result.__dict__}))
    await telemetry.shutdown(timeout_ms=2000)
asyncio.run(first())
release.set()
route=next(iter(runtime_telemetry._processors.values()))
route.pending.completion.result(timeout=2)
asyncio.run(second())
"""
    with tempfile.TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [sys.executable, "-c", script, str(new_events)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("queued_events", [False, True])
def test_shutdown_owns_processors_on_a_fresh_event_loop(queued_events: bool) -> None:
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    script = r"""
import asyncio,json,sys
from agents import telemetry
from _agents_codex import telemetry as runtime_telemetry
seen=[]
class Processor:
    def on_trace_event(self,event): seen.append((event['name'],event['lifecycle']))
async def configure(): await telemetry.configure(processors=[Processor()])
asyncio.run(configure())
if sys.argv[1]=='True':
    with telemetry.trace('between loops'): pass
asyncio.run(telemetry.shutdown(timeout_ms=2000))
expected=[('between loops','start'),('between loops','end')] if sys.argv[1]=='True' else []
assert seen==expected,seen
assert runtime_telemetry._processors=={} and runtime_telemetry._processor_task is None
print(json.dumps({'seen':seen,'shutdown':'complete'}))
"""
    with tempfile.TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [sys.executable, "-c", script, str(queued_events)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 0, result.stdout + result.stderr


def test_custom_scopes_deliver_on_a_fresh_loop_without_flush() -> None:
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    script = r"""
import asyncio,threading,gc,weakref
from agents import telemetry
seen=[]; delivered=threading.Event()
class Processor:
    def on_trace_event(self,event):
        seen.append((event['name'],event['lifecycle']))
        if len(seen)==2: delivered.set()
async def configure(): await telemetry.configure(processors=[Processor()])
async def scopes():
    with telemetry.trace('new loop only') as scope:
        assert telemetry.current_context() is scope.context
        reference=weakref.ref(scope.context)
    del scope
    assert await asyncio.to_thread(delivered.wait,2), seen
    assert seen==[('new loop only','start'),('new loop only','end')], seen
    gc.collect()
    assert reference() is None, 'processor pump retained application context'
    await telemetry.shutdown(timeout_ms=2000)
asyncio.run(configure())
asyncio.run(scopes())
"""
    with tempfile.TemporaryDirectory() as home:
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=15
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def _child_followup_trace(peer: DecodedCollector, directory: Path) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.native_fixtures import text_response
    from tests.test_native_child_agents import (
        agent_for,
        child_peer,
        collaboration,
        handles,
        metadata,
        trust,
    )
    from tests.test_native_executor_tools import declaration

    async def run_case(mode: str) -> tuple[int, bool, bool]:
        check_origin = mode.startswith("origin_")
        bare = mode in ("bare", "origin_bare_metadata")
        deferred = mode in (
            "deferred",
            "mixed",
            "origin_disabled",
            "origin_metadata",
            "origin_sensitive",
        )
        mixed = mode == "mixed"
        directory_for_case = directory / mode
        directory_for_case.mkdir()
        caller_id = {
            "idle": "a",
            "deferred": "b",
            "mixed": "c",
            "bare": "d",
            "origin_disabled": "1",
            "origin_metadata": "2",
            "origin_sensitive": "3",
            "origin_bare_metadata": "4",
            "origin_idle_metadata": "5",
        }[mode] * 32
        messages = ["follow-up input canary"]
        if mixed:
            messages = ["disabled queued input canary", "last input canary"]
        marker = f"FOLLOWUP_RESPONSE_CANARY_{mode}"
        completion_held, release_completion = asyncio.Event(), asyncio.Event()
        child_requests: list[dict[str, Any]] = []
        first_response_id = None
        mailbox_dispatched = asyncio.Event()
        loop = asyncio.get_running_loop()

        class DispatchObserver:
            def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
                if (
                    event["lifecycle"] == "end"
                    and event["trace_id"] == caller_id
                    and event["name"] == "op.dispatch.inter_agent_communication"
                ):
                    loop.call_soon_threadsafe(mailbox_dispatched.set)

        if deferred:
            telemetry.add_trace_processor(DispatchObserver())

        with pytest.MonkeyPatch.context() as patch:
            async with (
                child_peer(("default",)) as model,
                ExecutorPeer(directory_for_case / "remote").serve() as executor,
            ):
                trust(executor, directory_for_case, patch)
                root_response = model.response

                def response():
                    nonlocal first_response_id
                    body = model.requests[-1]
                    meta = metadata(body, model.request_headers[-1])
                    if meta["agent_name"] == "/root":
                        return root_response()
                    child_requests.append(meta)
                    if len(child_requests) == 1:
                        first_response_id = f"resp_{len(model.requests)}"
                        final = assistant_output("Initial final answer")
                        final["item"]["phase"] = "final_answer"
                        if not deferred:
                            return [final]
                        # Seeing the commentary delta proves Core has processed the preceding
                        # final answer, which defers mail to the next turn.
                        signal = text_response("after-final-answer", "After final answer")
                        signal[0]["item"]["phase"] = signal[-1]["item"]["phase"] = "commentary"
                        return [final, *signal]
                    inputs = json.dumps(body["input"])
                    assert all(inputs.count(message) == 1 for message in messages)
                    positions = [inputs.index(message) for message in messages]
                    assert positions == sorted(positions)
                    assert meta["turn_id"] != child_requests[0]["turn_id"]
                    final = assistant_output(marker)
                    final["item"]["phase"] = "final_answer"
                    return [final]

                async def hold_completion(event):
                    if (
                        deferred
                        and event["type"] == "response.completed"
                        and event["response"]["id"] == first_response_id
                    ):
                        completion_held.set()
                        await release_completion.wait()

                model.response = response
                model.before_event = hold_completion
                async with agent_for(model, executor).session(
                    environment=declaration(executor)
                ) as session:
                    try:
                        with telemetry.trace(
                            "spawn child with response capture",
                            options=telemetry.TraceOptions(
                                disabled=mode == "origin_disabled",
                                include_sensitive_data=not mode.endswith("metadata"),
                            ),
                        ):
                            creation = await session.submit("Spawn a child")
                        assert (await creation.get_result()).final_output == "root complete"
                        [original] = await handles(session, 1)
                        if deferred:
                            await completion_held.wait()
                            stream = original.events()
                            try:
                                async for event in stream:
                                    if (
                                        event.type == "output_text_delta"
                                        and event.item_id == "after-final-answer"
                                    ):
                                        break
                                else:
                                    pytest.fail("Core never observed the post-final commentary")
                            finally:
                                await stream.aclose()
                        else:
                            initial_result = await original.get_result()
                            assert initial_result.final_output == "Initial final answer"

                        if mixed:
                            # The enabled dispatch confirms the prior disabled send has
                            # been queued without waking the child.
                            model.action = collaboration(
                                "send_message",
                                {"target": "/root/alpha", "message": messages[0]},
                                "queued-call",
                            )
                            with telemetry.trace(
                                "queue-only caller",
                                options=telemetry.TraceOptions(
                                    disabled=True, include_sensitive_data=True
                                ),
                            ):
                                earlier = await session.submit("Queue mail for the child")
                            assert (await earlier.get_result()).final_output == "root complete"

                        model.action = collaboration(
                            "followup_task",
                            {"target": "/root/alpha", "message": messages[-1]},
                            "followup-call-last",
                        )
                        if bare:
                            assert telemetry.current_context() is None
                            caller_span_id = "123456789abcdef0"
                            wake = await session.submit(
                                "Follow up with the child",
                                parent_trace={
                                    "traceparent": f"00-{caller_id}-{caller_span_id}-01",
                                },
                            )
                        else:
                            with telemetry.trace(
                                "follow-up caller",
                                options=telemetry.TraceOptions(
                                    trace_id=caller_id, include_sensitive_data=mixed or check_origin
                                ),
                            ) as caller:
                                caller_span_id = caller.context.span_id
                                wake = await session.submit("Follow up with the child")
                        assert (await wake.get_result()).final_output == "root complete"

                        if deferred:
                            # The dispatch ends after mail is queued. Holding the old response
                            # until then forces the old task's completion to own the wake.
                            await asyncio.wait_for(mailbox_dispatched.wait(), timeout=8)
                            assert len(child_requests) == 1
                            release_completion.set()
                            await original.get_result()

                        async with timeout(8):
                            while True:
                                [successor] = await handles(session, 1)
                                if successor.turn_id != original.turn_id:
                                    break
                                await asyncio.sleep(0.01)
                            assert (await successor.get_result()).final_output == marker
                        assert successor.child_id == original.child_id
                        assert len(child_requests) == 2
                        assert child_requests[0]["turn_id"] == original.turn_id
                        assert child_requests[1]["turn_id"] == successor.turn_id
                        assert child_requests[1].get("parent_turn_id") == wake.turn_id
                    finally:
                        release_completion.set()

        assert (await telemetry.force_flush()).status == "success"
        spans = _spans(peer)
        successor_spans = [
            span
            for span in spans
            if span["name"] == "session_task.turn"
            and _attrs(span).get("turn.id") == successor.turn_id
        ]
        in_caller_trace = any(
            span["traceId"] == caller_id and _descends(span, caller_span_id, spans)
            for span in successor_spans
        )
        exported = any(
            marker in json.dumps(record["payload"])
            for record in peer.records
            if record["signal"] == "logs"
        )
        return len(successor_spans), in_caller_trace, exported

    results = {mode: await run_case(mode) for mode in ("idle", "deferred", "mixed")}
    assert results == {
        "idle": (1, True, False),
        "deferred": (1, True, False),
        "mixed": (0, False, False),
    }, ("follow-up span count, caller ancestry and response capture", results)
    bare = await run_case("bare")
    assert bare[:2] == (1, True), ("bare W3C follow-up span and ancestry", bare)

    origins = {
        mode: await run_case(mode)
        for mode in (
            "origin_disabled",
            "origin_metadata",
            "origin_sensitive",
            "origin_bare_metadata",
            "origin_idle_metadata",
        )
    }
    assert origins == {
        "origin_disabled": (0, False, False),
        "origin_metadata": (1, True, False),
        "origin_sensitive": (1, True, True),
        "origin_bare_metadata": (1, True, False),
        "origin_idle_metadata": (1, True, False),
    }, ("origin restrictions on a follow-up wake", origins)


async def _child_followup_reloaded_trace(collector: DecodedCollector, directory: Path) -> None:
    from dataclasses import replace

    from agents import ChildStepContext, NativeAgents
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, collaboration, metadata, trust
    from tests.test_native_executor_tools import declaration

    async def run_case(mode: str, index: int) -> tuple[int, bool, bool]:
        directory_for_case = directory / mode
        directory_for_case.mkdir()
        caller_id = f"{index:x}" * 32
        marker = f"RELOADED_FOLLOWUP_RESPONSE_{mode}"
        bare = mode == "cold_bare"
        cold_restore = mode in ("cold", "cold_bare")
        trace_state = "vendor=synthetic"
        carriers: list[tuple[dict[str, str], bool]] = []
        model_carriers: list[dict[str, str]] = []

        @function_tool
        def read_carrier(ctx: ToolContext[None]) -> str:
            """Read a synthetic value."""
            assert ctx.trace_context is not None
            carriers.append((ctx.trace_context.to_headers(), telemetry.current_context() is None))
            return "synthetic value"

        async def supply_tool(_step: ChildStepContext[None]) -> list[FunctionTool]:
            return [read_carrier]

        followup_input = "wake the original child once"
        requests: dict[str, list[dict[str, Any]]] = {"alpha": [], "beta": []}

        with pytest.MonkeyPatch.context() as patch:
            async with (
                child_peer(("default",)) as model,
                ExecutorPeer(directory_for_case / "remote").serve() as executor,
            ):
                trust(executor, directory_for_case, patch)
                original_response = model.response

                def response():
                    body = model.requests[-1]
                    meta = metadata(body, model.request_headers[-1])
                    if meta["agent_name"] == "/root":
                        return original_response()
                    name = meta["agent_name"].rsplit("/", 1)[-1]
                    requests[name].append(meta)
                    if name == "beta":
                        return [assistant_output("Replacement complete")]
                    if len(requests["alpha"]) == 1:
                        return [assistant_output("Initial complete")]
                    assert json.dumps(body["input"]).count(followup_input) == 1
                    if bare:
                        client_metadata = body["client_metadata"]
                        model_carriers.append(
                            {
                                key: client_metadata[f"ws_request_header_{key}"]
                                for key in ("traceparent", "tracestate")
                                if f"ws_request_header_{key}" in client_metadata
                            }
                        )
                        if len(requests["alpha"]) == 2:
                            return [function_call("read_carrier", {}, "bare-followup-tool")]
                    final = assistant_output(marker)
                    final["item"]["phase"] = "final_answer"
                    return [final]

                model.response = response
                agent = agent_for(model, executor, websocket=bare)
                [native] = [tool for tool in agent.tools if isinstance(tool, NativeAgents)]
                native = replace(native, max_children=1)
                agent = replace(
                    agent,
                    tools=[read_carrier, replace(native, before_step=supply_tool)]
                    if bare
                    else [native],
                )
                environment = declaration(executor)

                async def child_at(session, name, *, settled=False):
                    async with timeout(8):
                        while True:
                            if requests[name]:
                                child_id = requests[name][0]["thread_id"]
                                async for child in session.subagents():
                                    if child.id == child_id and (
                                        not settled or child.turn is not None
                                    ):
                                        return child
                            await asyncio.sleep(0)

                async def wake(session):
                    model.action = collaboration(
                        "followup_task",
                        {"target": "/root/alpha", "message": followup_input},
                        "reload-alpha",
                    )
                    if bare:
                        assert telemetry.current_context() is None
                        caller_span_id = "123456789abcdef0"
                        root = await session.submit(
                            "Wake alpha again",
                            parent_trace={
                                "traceparent": f"00-{caller_id}-{caller_span_id}-01",
                                "tracestate": trace_state,
                            },
                        )
                    else:
                        with telemetry.trace(
                            "reload caller",
                            options=telemetry.TraceOptions(
                                trace_id=caller_id, include_sensitive_data=True
                            ),
                        ) as scope:
                            root = await session.submit("Wake alpha again")
                            caller_span_id = scope.context.span_id
                    assert (await root.get_result()).final_output == "root complete"
                    replacement = await child_at(session, "alpha", settled=True)
                    assert replacement.id == alpha.id
                    assert replacement.turn is not None
                    if not cold_restore:
                        assert replacement.turn.instance_id != first.instance_id
                    assert replacement.turn.turn_id != first.turn_id
                    assert (await replacement.turn.get_result()).final_output == marker
                    assert len(requests["alpha"]) == (3 if bare else 2)
                    if bare:
                        assert requests["alpha"][1]["turn_id"] == replacement.turn.turn_id
                        [(carrier, no_private_scope)] = carriers
                        assert no_private_scope
                        assert len(model_carriers) == 2
                        for context in [carrier, *model_carriers]:
                            assert context["traceparent"][3:35] == caller_id
                            assert context["traceparent"].endswith("-00")
                        assert [
                            context.get("tracestate") for context in [carrier, *model_carriers]
                        ] == [trace_state] * 3, (carrier, model_carriers)
                    assert requests["alpha"][-1]["turn_id"] == replacement.turn.turn_id
                    assert requests["alpha"][-1].get("parent_turn_id") == root.turn_id
                    return replacement.turn.turn_id, caller_span_id

                async with agent.session(environment=environment) as session:
                    with telemetry.trace(
                        "create alpha",
                        options=telemetry.TraceOptions(
                            disabled=mode == "disabled",
                            include_sensitive_data=mode != "metadata",
                        ),
                    ):
                        creation = await session.submit("Create alpha")
                    assert (await creation.get_result()).final_output == "root complete"
                    alpha = await child_at(session, "alpha", settled=True)
                    first = alpha.turn
                    assert first is not None
                    assert (await first.get_result()).final_output == "Initial complete"
                    if cold_restore:
                        saved = await session.save_state()
                    else:
                        model.action = collaboration(
                            "spawn_agent",
                            {"task_name": "beta", "message": "Replace alpha", "fork_turns": "none"},
                            "evict-alpha",
                        )
                        root = await session.submit("Make alpha nonresident")
                        assert (await root.get_result()).final_output == "root complete"
                        beta = await child_at(session, "beta", settled=True)
                        assert beta.turn is not None
                        assert (await beta.turn.get_result()).final_output == "Replacement complete"
                        evicted = await child_at(session, "alpha")
                        assert not evicted.is_live and evicted.turn is None
                        turn_id, caller_span_id = await wake(session)
                if cold_restore:
                    async with agent.session(environment=environment, state=saved) as restored:
                        cold = await child_at(restored, "alpha")
                        assert cold.id == alpha.id and not cold.is_live and cold.turn is None
                        turn_id, caller_span_id = await wake(restored)

        assert (await telemetry.force_flush()).status == "success"
        spans = _spans(collector)
        successor_spans = [
            span
            for span in spans
            if span["name"] == "session_task.turn" and _attrs(span).get("turn.id") == turn_id
        ]
        ancestry = any(
            span["traceId"] == caller_id and _descends(span, caller_span_id, spans)
            for span in successor_spans
        )
        captured = any(
            marker in json.dumps(record["payload"])
            for record in collector.records
            if record["signal"] == "logs"
        )
        return len(successor_spans), ancestry, captured

    observed = {
        mode: await run_case(mode, index)
        for index, mode in enumerate(("disabled", "metadata", "permissive", "cold", "cold_bare"), 6)
    }

    assert observed == {
        "disabled": (0, False, False),
        "metadata": (1, True, False),
        "permissive": (1, True, True),
        "cold": (0, False, False),
        "cold_bare": (0, False, False),
    }, ("reloaded child tracing and response capture", observed)


async def _child_turn_policy(recorder: Recorder, peer: DecodedCollector, directory: Path) -> None:
    from dataclasses import replace

    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    seen = []
    for index, mode in enumerate(("disabled", "metadata", "origin_disabled"), 1):
        root = directory / mode
        root.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            async with (
                child_peer(("default",)) as model,
                ExecutorPeer(root / "remote").serve() as executor,
            ):
                trust(executor, root, patch)
                seen.clear()
                origin_id = str(index) * 32
                disabled = mode != "metadata"

                @function_tool
                def inspect_child_policy(label: str) -> str:
                    context = telemetry.current_context()
                    assert context is not None
                    seen.append(
                        (label, context.trace_id, context.disabled, context.include_sensitive_data)
                    )
                    return "observed"

                original = model.response
                fresh, steer = "FRESH_CHILD_PAYLOAD_" + mode, "STEER_CHILD_PAYLOAD_" + mode

                def response(original=original, fresh=fresh, steer=steer):
                    body = model.requests[-1]
                    if metadata(body, model.request_headers[-1])["agent_name"] == "/root":
                        return original()
                    encoded = json.dumps(body["input"])
                    label = (
                        "fresh" if fresh in encoded else "steer" if steer in encoded else "initial"
                    )
                    call_id = "policy-" + label
                    if not any(
                        item.get("type") == "function_call_output"
                        and item.get("call_id") == call_id
                        for item in body["input"]
                    ):
                        return [function_call("inspect_child_policy", {"label": label}, call_id)]
                    answer = assistant_output("alpha complete")
                    answer["item"]["id"] = "done-alpha"
                    return [answer]

                model.response = response
                agent = agent_for(model, executor)
                agent = replace(agent, tools=[*agent.tools, inspect_child_policy])
                async with agent.session(
                    environment=declaration(executor),
                    trace_options=telemetry.TraceOptions(
                        disabled=mode == "disabled", include_sensitive_data=mode != "metadata"
                    ),
                ) as session:
                    with telemetry.trace(
                        "child creation",
                        options=telemetry.TraceOptions(
                            trace_id=origin_id,
                            disabled=mode == "origin_disabled",
                            include_sensitive_data=True,
                        ),
                    ) as creation_scope:
                        initial = await session.submit("Create child")
                    assert (await initial.get_result()).final_output == "root complete"
                    [first] = await handles(session, 1)
                    initial_wait = asyncio.create_task(first.get_result())
                    entered_wait = asyncio.create_task(model.entered["alpha"].wait())
                    try:
                        ready, _ = await asyncio.wait(
                            (initial_wait, entered_wait), return_when=asyncio.FIRST_COMPLETED
                        )
                        if initial_wait in ready:
                            try:
                                await initial_wait
                            except Exception as error:
                                raise AssertionError(
                                    (
                                        mode,
                                        len(model.requests),
                                        seen,
                                        "child failed before its response hold",
                                    )
                                ) from error
                            raise AssertionError(
                                (
                                    mode,
                                    len(model.requests),
                                    seen,
                                    "child completed before its response hold",
                                )
                            )
                        with telemetry.trace(
                            "unrelated child caller",
                            options=telemetry.TraceOptions(
                                trace_id="f" * 32, include_sensitive_data=True
                            ),
                        ):
                            await first.steer(steer)
                            model.release["alpha"].set()
                            assert (await initial_wait).final_output == "alpha complete"
                            [child] = [child async for child in session.subagents()]
                            later = await child.start(fresh)
                            assert later.turn_id != first.turn_id
                            assert (await later.get_result()).final_output == "alpha complete"
                    finally:
                        model.release["alpha"].set()
                        for waiter in (initial_wait, entered_wait):
                            if not waiter.done():
                                waiter.cancel()
                        await asyncio.gather(initial_wait, entered_wait, return_exceptions=True)
                    assert {row[0] for row in seen} == {"initial", "steer", "fresh"}, seen
                    assert all(
                        row[1:] == (origin_id, disabled, mode != "metadata") for row in seen
                    ), seen
                assert (await telemetry.force_flush(timeout_ms=5000)).status == "success"
                origin_events = [
                    event for event in recorder.events if event["trace_id"] == origin_id
                ]
                caller_events = [
                    event
                    for event in origin_events
                    if event["span_id"] == creation_scope.context.span_id
                ]
                # The independent caller workflow may record even when the
                # session/child is disabled. It does not grant child export.
                assert bool(caller_events) == (mode != "origin_disabled")
                if caller_events:
                    assert {event["lifecycle"] for event in caller_events} == {"start", "end"}
                assert all(
                    event["name"] == "child creation"
                    and event["parent_span_id"] == creation_scope.context.parent_span_id
                    for event in caller_events
                )
                if disabled:
                    assert origin_events == caller_events
                else:
                    assert any(
                        event["span_id"] != creation_scope.context.span_id
                        for event in origin_events
                    )
                    assert fresh not in json.dumps(peer.records)
                    assert steer not in json.dumps(peer.records)


async def _child_interaction_policy(directory: Path) -> None:
    from dataclasses import replace

    from agents import ApprovalRequest, ElicitationRequest
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration
    from tests.test_native_mcp_tools import mcp_call
    from tests.test_native_owned_child_prompts import interactive
    from tests.test_native_scoped_policies import AskingPeer

    for index, (mode, route) in enumerate(
        (("disabled", "exec"), ("metadata", "form"), ("origin_disabled", "exec")), 4
    ):
        root = directory / mode
        root.mkdir()
        vault = AskingPeer()
        vault.annotations = None
        with pytest.MonkeyPatch.context() as patch:
            async with (
                child_peer(("default",)) as model,
                ExecutorPeer(root / "remote").serve() as executor,
                vault.serve() as url,
            ):
                trust(executor, root, patch)
                model.command = "printf never-approved"
                model.extra_arguments = {
                    "sandbox_permissions": "require_escalated",
                    "justification": "Synthetic denial",
                }
                model.release["alpha"].set()
                original = model.response

                def response(original=original, route=route):
                    body = model.requests[-1]
                    if (
                        route == "exec"
                        or metadata(body, model.request_headers[-1])["agent_name"] == "/root"
                    ):
                        return original()
                    if not any(
                        item.get("type") == "function_call_output"
                        and item.get("call_id") == "form-call"
                        for item in body["input"]
                    ):
                        return [mcp_call("search", "mcp__vault", "form-call")]
                    return [assistant_output("alpha complete")]

                model.response = response
                agent = interactive(agent_for(model, executor))
                if route == "form":
                    agent = replace(
                        agent,
                        tools=[
                            *agent.tools,
                            HttpMCP(
                                name="vault", url=url, tools=["search"], approval_mode="preapproved"
                            ),
                        ],
                    )
                origin_id = str(index) * 32
                observed = []
                retained_questions = []
                async with agent.session(
                    environment=declaration(executor),
                    trace_options=telemetry.TraceOptions(
                        disabled=mode == "disabled", include_sensitive_data=mode != "metadata"
                    ),
                ) as session:
                    with telemetry.trace(
                        "question origin",
                        options=telemetry.TraceOptions(
                            trace_id=origin_id,
                            disabled=mode == "origin_disabled",
                            include_sensitive_data=True,
                        ),
                    ):
                        initial = await session.submit("Create child question")
                    assert (await initial.get_result()).final_output == "root complete"
                    [child_turn] = await handles(session, 1)

                    async def handler(
                        question,
                        child_turn=child_turn,
                        origin_id=origin_id,
                        mode=mode,
                        observed=observed,
                        retained_questions=retained_questions,
                    ):
                        assert question.subagent_id == child_turn.child_id
                        assert question.turn_id == child_turn.turn_id
                        current = telemetry.current_context()
                        assert current is not None and current.trace_id == origin_id
                        assert current.disabled == (mode != "metadata")
                        assert current.include_sensitive_data == (mode != "metadata")
                        with telemetry.trace(
                            "nested question work",
                            options=telemetry.TraceOptions(
                                disabled=False, include_sensitive_data=True, detached=True
                            ),
                        ) as nested:
                            assert nested.context.disabled == current.disabled
                            assert (
                                nested.context.include_sensitive_data
                                == current.include_sensitive_data
                            )
                        observed.append(question.type)
                        retained_questions.append(question)
                        if isinstance(question, ApprovalRequest):
                            return question.reply("deny")
                        assert isinstance(question, ElicitationRequest)
                        return question.reply({"answer": "accepted"})

                    # The public one-shot path delegates here. Use the live child
                    # as waiter so callbacks run after the root has completed.
                    with telemetry.trace(
                        "unrelated question caller",
                        options=telemetry.TraceOptions(
                            trace_id="f" * 32, include_sensitive_data=True
                        ),
                    ) as unrelated:
                        answer = await session._run_with_interactions(child_turn, handler)
                        assert telemetry.current_context() is unrelated.context
                    assert answer.final_output == "alpha complete"
                    assert observed == [
                        "approval_request" if route == "exec" else "elicitation_request"
                    ]
                    assert all(
                        question._tracing is None and "tracing_context" not in question._message
                        for question in retained_questions
                    )
                    if route == "form":
                        assert vault.answers[0]["result"]["content"] == {"answer": "accepted"}
