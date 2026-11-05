"""REQ423–429 qualification through the real Python/native/collector boundaries.

Only the local model is scripted. Each case owns a fresh native subscriber;
processor, callback, queue and export behavior run without native mocks.
"""

from __future__ import annotations

import asyncio
import base64
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
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    NativeProvider,
    OutputValidationError,
    TurnStoppedError,
    function_tool,
    telemetry,
)
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
                    if case in ("policies", "ancestry_queue")
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
                else:
                    raise AssertionError(case)
            finally:
                await provider.aclose()
                await telemetry.shutdown(timeout_ms=5000)


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
        assert not first.done()
    finally:
        release.set()
        await first


async def _manual_restore(
    fixture: ResponsesFixture, provider: NativeProvider, peer: DecodedCollector
) -> None:
    class Arguments(BaseModel):
        key: str

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
    assert source_scope.context._handle not in serialized
    assert "tracing_context" not in serialized
    assert "credential_ref" not in serialized
    assert "default-key" not in serialized
    _answer(fixture, "RESTORED_OUTPUT_CANARY")
    with telemetry.trace(
        "restored workflow",
        options=telemetry.TraceOptions(
            include_sensitive_data=False,
        ),
    ) as restored_scope:
        async with agent.session(state=saved) as restored:
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
    assert result.failed_deliveries==2, result
    again=await telemetry.force_flush(timeout_ms=2000)
    assert again.status=='partial' and again.failed_deliveries==2, again
    assert [name for _,name,lifecycle in seen if name=='first']==['first']
    if sys.argv[1]=='True':
        second_events=[(name,lifecycle) for _,name,lifecycle in seen if name=='second']
        assert second_events==[('second','start'),('second','end')]
    print(json.dumps({'seen':seen,'flush':result.__dict__}))
    await telemetry.shutdown(timeout_ms=2000)
asyncio.run(first())
release.set()
route=next(iter(telemetry._processors.values()))
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
assert telemetry._processors=={} and telemetry._processor_task is None
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
