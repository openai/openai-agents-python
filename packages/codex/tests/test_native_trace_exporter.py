"""Native capture policy and traces-ingest transport, with a local HTTP collector."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import Thread
from typing import Any

import pytest

from agents import Agent, telemetry
from agents.testing import ScriptedResponses, text
from tests._asyncio import capture_cancellation


@contextmanager
def collector(
    statuses: list[int] | None = None, *, delay: float = 0
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received = []
    replies = list(statuses or [])

    class LoopbackHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            received.append(
                {
                    "headers": dict(self.headers),
                    "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            if delay:
                import time

                time.sleep(delay)
            self.send_response(replies.pop(0) if replies else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces/ingest", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def assert_sanitized_failure(error, secret, *, expected_context=None):
    """Inspect exception objects and library frames, including suppressed context."""
    assert error.__cause__ is None
    assert error.__context__ is expected_context
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            module = traceback.tb_frame.f_globals.get("__name__", "")
            if module.startswith(("_agents_codex", "httpx2", "httpcore2", "asyncio")):
                for value in traceback.tb_frame.f_locals.values():
                    # Caller-owned objects are outside this diagnostic boundary.
                    if isinstance(value, (str, bytes, dict, list, tuple)):
                        assert secret not in repr(value)
            traceback = traceback.tb_next
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )


def completed_event():
    return {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "parent_span_id": None,
        "name": "synthetic operation",
        "lifecycle": "end",
        "outcome": "error",
        "start_time_unix_nanos": "1700000000123456789",
        "end_time_unix_nanos": "1700000001987654321",
        "attributes": {"sdk.outcome.category": "synthetic_error"},
    }


async def test_exporter_retries_bounded_http_failure_and_preserves_wire_identity():
    with collector([503, 200]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-key", endpoint=endpoint, max_retries=1
        )
        await exporter.on_trace_event(completed_event())
    assert len(received) == 2
    assert received[-1]["headers"]["Authorization"] == "Bearer synthetic-key"
    assert received[-1]["headers"]["OpenAI-Beta"] == "traces=v1"
    trace, span = received[-1]["payload"]["data"]
    assert trace["id"] == "trace_" + "a" * 32
    assert span["id"] == "span_" + "0" * 8 + "b" * 16
    assert span["parent_id"] is None
    assert span["started_at"] == "2023-11-14T22:13:20.123456789Z"
    assert span["ended_at"] == "2023-11-14T22:13:21.987654321Z"
    assert span["error"]["data"]["outcome"] == "error"


@pytest.mark.parametrize("status", [302, 403, 429])
async def test_exporter_rejects_redirects_and_terminal_failure_without_secret_diagnostics(status):
    with collector([status]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-private-key", endpoint=endpoint, max_retries=3
        )
        with pytest.raises(RuntimeError) as failure:
            await exporter.on_trace_event(completed_event())
        assert str(failure.value) == "Native OpenAI trace export failed"
        assert_sanitized_failure(failure.value, "synthetic-private-key")
        assert len(received) == 1


def native_trace_child_env() -> dict[str, str]:
    env = os.environ.copy()
    test_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (test_root, env.get("PYTHONPATH"))))
    return env


@pytest.mark.parametrize(
    "case_name",
    [
        "native_case",
        "native_external_parent_case",
        "native_deadline_case",
        "native_deadline_reset_case",
        "native_outcome_privacy_case",
        "native_auth_case",
        "native_auth_destination_case",
        "native_auth_replacement_case",
        pytest.param("native_batch_case", marks=pytest.mark.serial),
    ],
)
def test_real_native_capture_policy_and_openai_export(case_name):
    env = native_trace_child_env()
    env.pop("OPENAI_API_KEY", None)
    runner = "asyncio.run(case)"
    if (
        sys.platform == "win32"
        and sys.version_info >= (3, 12)
        and case_name in {"native_deadline_case", "native_deadline_reset_case"}
    ):
        # A peer reset can interrupt Proactor transport teardown in this loopback server.
        runner = "asyncio.run(case, loop_factory=asyncio.SelectorEventLoop)"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests import test_native_trace_exporter as cases; "
            "case = getattr(cases, sys.argv[1])(); " + runner,
            case_name,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case():
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        diagnostics = io.StringIO()
        await telemetry.configure(
            processors=[Recorder(), exporter, telemetry.ConsoleTraceProcessor(stream=diagnostics)]
        )
        with telemetry.trace(
            "allowed",
            options={
                "include_sensitive_data": True,
                "metadata": {"tenant": "native-tenant"},
                "group_id": "native-group",
            },
        ):
            telemetry.set_tracing_disabled(True)
            with telemetry.span("suppressed", options={"disabled": False}):
                pass
            with telemetry.trace("disabled-parent"):
                telemetry.set_tracing_disabled(False)
                with telemetry.span("still-suppressed"):
                    pass
            telemetry.set_sensitive_data_logging_enabled(False)
            with telemetry.span(
                "metadata-only", options={"include_sensitive_data": True}
            ) as active:
                assert not active.context.include_sensitive_data
                active.set_outcome(status="ok", data={"private_fixture": "must-not-export"})
            telemetry.set_sensitive_data_logging_enabled(True)
            with telemetry.span("with-payload", options={"include_sensitive_data": True}) as active:
                active.set_outcome(
                    status="error",
                    category="synthetic_error",
                    data={"result": "synthetic completion"},
                )
        telemetry.set_tracing_disabled(True)
        async with ScriptedResponses(lambda body: [text("native dynamic answer")]) as peer:
            agent = Agent(instructions="Respond.", model="gpt-5.5", provider=peer.provider)
            assert (await agent.run("synthetic input")).final_output == "native dynamic answer"
        telemetry.set_tracing_disabled(False)
        flushed = await telemetry.force_flush()
        assert flushed.status == "success", flushed
        names = [event["name"] for event in events]
        assert "allowed" in names
        for name in ("suppressed", "disabled-parent", "still-suppressed", "operation"):
            assert name not in names
        completed = next(
            event
            for event in events
            if event["name"] == "with-payload" and event["lifecycle"] == "end"
        )
        records = [record for request in received for record in request["payload"]["data"]]
        root_record = next(record for record in records if record["object"] == "trace")
        assert root_record["metadata"]["tenant"] == "native-tenant"
        assert root_record["group_id"] == "native-group"
        assert "must-not-export" not in json.dumps(records)
        assert "with-payload" in diagnostics.getvalue()
        assert "must-not-export" not in diagnostics.getvalue()
        exported = next(
            record
            for record in records
            if record.get("span_data", {}).get("name") == "with-payload"
        )
        assert exported["id"] == "span_" + completed["span_id"].rjust(24, "0")
        assert exported["trace_id"] == "trace_" + completed["trace_id"]
        assert exported["parent_id"] == "span_" + completed["parent_span_id"].rjust(24, "0")
        assert "synthetic completion" in exported["span_data"]["data"]["sdk.outcome.data"]
        await telemetry.shutdown()


async def native_external_parent_case():
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    local_id = "1" * 32
    remote_id = "2" * 32
    remote_parent = "3" * 16
    with collector() as (endpoint, received):
        await telemetry.configure(
            processors=[
                Recorder(),
                telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint),
            ]
        )
        try:
            with telemetry.trace("local workflow", options={"trace_id": local_id}):
                pass
            assert (await telemetry.force_flush()).status == "success"
            assert any(
                record.get("id") == f"trace_{local_id}"
                for request in received
                for record in request["payload"]["data"]
            )

            async with ScriptedResponses([text("synthetic result")]) as peer:
                result = await Agent(instructions="Respond.", provider=peer.provider).run(
                    "synthetic request",
                    parent_trace={"traceparent": f"00-{remote_id}-{remote_parent}-01"},
                )
                assert result.final_output == "synthetic result"
            flush = await telemetry.force_flush()
            assert flush.status == "partial"
            assert flush.failed_deliveries > 0
            assert flush.dropped_records == 0
            remote_events = [event for event in events if event["trace_id"] == remote_id]
            assert any(
                event["parent_span_id"] == remote_parent and event["lifecycle"] == "end"
                for event in remote_events
            )
            records = [record for request in received for record in request["payload"]["data"]]
            assert all(
                record.get("id") != f"trace_{remote_id}"
                and record.get("trace_id") != f"trace_{remote_id}"
                for record in records
            )
        finally:
            await telemetry.shutdown()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_export_deadline_and_cancellation_close_trickling_connection(cancelled):
    import asyncio
    import time

    peer_closed = asyncio.Event()
    handler_done = asyncio.Event()
    request_started = asyncio.Event()
    handlers = set()

    async def bounded(awaitable, phase):
        try:
            return await asyncio.wait_for(awaitable, 3)
        except asyncio.TimeoutError:
            raise AssertionError(f"Trace exporter fixture stalled during {phase}") from None

    async def trickle(reader, writer):
        handlers.add(asyncio.current_task())
        eof = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            await reader.readexactly(length)
            request_started.set()
            writer.write(b"HTTP/1.1 200 OK\r\n")
            await writer.drain()
            eof = asyncio.create_task(reader.read())
            for _ in range(30):
                done, _ = await asyncio.wait((eof,), timeout=0.02)
                if done:
                    assert eof.result() == b""
                    peer_closed.set()
                    break
                writer.write(b"X-Synthetic: header\r\n")
                await writer.drain()
            else:
                writer.write(b"Content-Length: 0\r\n\r\n")
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            if not request_started.is_set():
                raise
            peer_closed.set()
        finally:
            if eof is not None:
                if not eof.done():
                    eof.cancel()
                await bounded(asyncio.gather(eof, return_exceptions=True), "reader cleanup")
            writer.close()
            try:
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except asyncio.TimeoutError:
                    writer.transport.abort()
                    raise AssertionError(
                        "Trace exporter fixture stalled during writer close"
                    ) from None
            except (BrokenPipeError, ConnectionResetError):
                if not peer_closed.is_set():
                    raise
            handler_done.set()

    if not cancelled:
        with collector() as (warmup_endpoint, requests):
            warmup = telemetry.OpenAITraceExporter(
                api_key="synthetic-key", endpoint=warmup_endpoint
            )
            await bounded(warmup.on_trace_event(completed_event()), "warmup delivery")
            assert len(requests) == 1

    server = await asyncio.start_server(trickle, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/traces/ingest"
    secret = "synthetic-private-timeout-key"
    exporter = telemetry.OpenAITraceExporter(
        api_key=secret, endpoint=endpoint, timeout_ms=900 if cancelled else 200
    )
    event = completed_event()
    event["attributes"]["private_fixture"] = secret
    started = time.monotonic()
    cancellations = []
    delivery = asyncio.create_task(
        capture_cancellation(exporter.on_trace_event(event), cancellations)
    )
    try:
        if cancelled:
            await asyncio.wait_for(request_started.wait(), 1)
            # Cancellation latency, excluding transport startup.
            started = time.monotonic()
            delivery.cancel("synthetic caller cancellation")
            with pytest.raises(asyncio.CancelledError) as failure:
                await bounded(delivery, "trickling delivery")
            assert delivery.cancelled()
            assert [error.args for error in cancellations] == [("synthetic caller cancellation",)]
            assert_sanitized_failure(cancellations[0], secret)
            # Python 3.10 wraps the original cancellation when the Task finishes.
            assert_sanitized_failure(
                failure.value,
                secret,
                expected_context=None if failure.value is cancellations[0] else cancellations[0],
            )
        else:
            with pytest.raises(RuntimeError, match="Native OpenAI trace export failed") as failure:
                await bounded(delivery, "trickling delivery")
            assert_sanitized_failure(failure.value, secret)
        # The old per-read timeout could succeed after roughly 600 ms of headers.
        assert time.monotonic() - started < 0.4
        await asyncio.wait_for(peer_closed.wait(), 0.3)
        await asyncio.wait_for(handler_done.wait(), 0.3)
        assert delivery.done()
        # The current callback loop remains usable for a subsequent delivery.
        with collector() as (healthy_endpoint, requests):
            recovered = telemetry.OpenAITraceExporter(
                api_key="synthetic-key", endpoint=healthy_endpoint
            )
            await bounded(recovered.on_trace_event(completed_event()), "recovery delivery")
            assert len(requests) == 1
    finally:
        if not delivery.done():
            delivery.cancel()
            await bounded(asyncio.gather(delivery, return_exceptions=True), "delivery cleanup")
        server.close()
        await bounded(server.wait_closed(), "server close")
        if handlers:
            await bounded(asyncio.gather(*handlers), "handler cleanup")


async def native_deadline_case():
    import asyncio

    requests = []
    first_closed = asyncio.Event()
    handlers = set()

    async def receive(reader, writer):
        handler = asyncio.current_task()
        handlers.add(handler)
        eof = None
        trickling = False
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(length)))
            writer.write(b"HTTP/1.1 200 OK\r\n")
            trickling = len(requests) == 2
            if trickling:
                eof = asyncio.create_task(reader.read())
                for _ in range(30):
                    done, _ = await asyncio.wait((eof,), timeout=0.02)
                    if done:
                        assert eof.result() == b""
                        first_closed.set()
                        return
                    writer.write(b"X-Synthetic: header\r\n")
                    await writer.drain()
            writer.write(b"Content-Length: 0\r\n\r\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            if not trickling:
                raise
            first_closed.set()
        finally:
            if eof is not None and not eof.done():
                eof.cancel()
                await asyncio.gather(eof, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                if not trickling:
                    raise
                first_closed.set()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/traces/ingest"
    configured = False
    try:
        # Warm the lazy HTTP transport before the bounded native request.
        warmup = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await warmup.on_trace_event(completed_event())
        assert len(requests) == 1
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-key", endpoint=endpoint, timeout_ms=200
        )
        await telemetry.configure(processors=[exporter])
        configured = True
        with telemetry.trace("deadline-first"):
            pass
        flushed = await telemetry.force_flush()
        assert flushed.status == "partial"
        assert flushed.failed_deliveries >= 1
        await asyncio.wait_for(first_closed.wait(), 0.3)
        with telemetry.trace("recovered-after-deadline"):
            pass
        await telemetry.force_flush()
        records = [record for request in requests for record in request["data"]]
        assert any(
            record.get("span_data", {}).get("name") == "recovered-after-deadline"
            for record in records
        )
    finally:
        try:
            if configured:
                await telemetry.shutdown()
        finally:
            server.close()
            # Accepted streams can keep wait_closed pending until their handlers close them.
            for handler in handlers:
                if not handler.done():
                    handler.cancel()
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*handlers, return_exceptions=True), 1
                )
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        raise result
            finally:
                await asyncio.wait_for(server.wait_closed(), 1)


async def native_deadline_reset_case():
    import asyncio

    read = asyncio.StreamReader.read

    async def reset_on_eof(reader, n=-1):
        data = await read(reader, n)
        if data == b"":
            raise ConnectionResetError("synthetic Windows peer close")
        return data

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio.StreamReader, "read", reset_on_eof)
        await native_deadline_case()


async def test_failed_root_descriptor_metadata_and_rotation_survive_delivery():
    root = {
        **completed_event(),
        "name": "root-workflow",
        "lifecycle": "start",
        "parent_span_id": None,
        "attributes": {
            "sdk.group_id": "root-group",
            "sdk.metadata.tenant": "synthetic-tenant",
            "sdk.metadata.literal": "true",
        },
    }
    child = {**completed_event(), "parent_span_id": "c" * 16, "name": "child-span"}
    with collector([503, 200, 200]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-old-key", endpoint=endpoint)
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event(root)
        await exporter.on_trace_event(child)
        exporter.set_api_key("synthetic-new-key")
        await exporter.on_trace_event(child)
    for request in received[1:]:
        trace = request["payload"]["data"][0]
        assert trace["object"] == "trace"
        assert trace["workflow_name"] == "root-workflow"
        assert trace["group_id"] == "root-group"
        assert trace["metadata"] == {
            "runtime": "native",
            "tenant": "synthetic-tenant",
            "literal": "true",
        }
    assert received[-1]["headers"]["Authorization"] == "Bearer synthetic-new-key"


async def test_batch_delivery_preserves_order_and_bounded_descriptor_lifetime():
    events = [
        {**completed_event(), "span_id": f"{index:016x}", "name": f"span-{index}"}
        for index in range(30)
    ]
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await exporter.on_trace_events(events)
        assert len(received) == 1
        records = received[0]["payload"]["data"]
        assert [row["span_data"]["name"] for row in records if row["object"] == "trace.span"] == [
            f"span-{index}" for index in range(30)
        ]
        roots = [
            {
                **completed_event(),
                "lifecycle": "end",
                "parent_span_id": None,
                "trace_id": f"{index:032x}",
            }
            for index in range(1100)
        ]
        await exporter.on_trace_events(roots)
        assert len(exporter._traces) == 1024


async def test_credential_rotation_during_old_delivery_resends_root():
    import asyncio
    from threading import Event

    entered = Event()
    release = Event()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(
                {
                    "headers": dict(self.headers),
                    "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            if len(received) == 1:
                entered.set()
                release.wait(2)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    task = None
    try:
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-old-key", endpoint=f"http://127.0.0.1:{server.server_port}/ingest"
        )
        root = {
            **completed_event(),
            "name": "root-workflow",
            "lifecycle": "start",
            "parent_span_id": None,
        }
        task = asyncio.create_task(exporter.on_trace_event(root))
        assert await asyncio.to_thread(entered.wait, 1)
        exporter.set_api_key("synthetic-new-key")
        release.set()
        await task
        await exporter.on_trace_event(completed_event())
        assert received[0]["headers"]["Authorization"] == "Bearer synthetic-old-key"
        assert received[1]["headers"]["Authorization"] == "Bearer synthetic-new-key"
        assert received[1]["payload"]["data"][0]["workflow_name"] == "root-workflow"
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        server.shutdown()
        server.server_close()
        worker.join()


async def native_outcome_privacy_case():
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    await telemetry.configure(processors=[Recorder()])
    secret = "synthetic-private-outcome"
    scope = telemetry.span("bounded-outcome")
    with pytest.raises(RuntimeError, match="Native trace span end failed") as failure:
        with scope:
            scope.set_outcome(status="ok", data={"private": secret * 2000})
    assert_sanitized_failure(failure.value, secret)
    assert scope._outcome is None
    await telemetry.force_flush()
    assert secret not in repr(events)
    await telemetry.shutdown()


async def native_batch_case():
    import time

    with collector() as (warmup_endpoint, warmup_received):
        warmup = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=warmup_endpoint)
        await warmup.on_trace_event(completed_event())
        assert len(warmup_received) == 1

    with collector(delay=0.5) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await telemetry.configure(processors=[exporter])
        with telemetry.trace("batched-workflow"):
            for index in range(20):
                with telemetry.span(f"batched-{index}"):
                    pass
        started = time.monotonic()
        result = await telemetry.force_flush()
        elapsed = time.monotonic() - started
        assert result.status == "success", result
        records = [record for request in received for record in request["payload"]["data"]]
        spans = [record for record in records if record["object"] == "trace.span"]
        assert len(spans) == 21
        assert [row["span_data"]["name"] for row in spans] == [
            f"batched-{index}" for index in range(20)
        ] + ["batched-workflow"]
        assert len(received) <= 3
        assert elapsed < 4
        print(
            json.dumps(
                {
                    "native_completed_spans": len(spans),
                    "http_requests": len(received),
                    "flush_seconds": round(elapsed, 3),
                    "flush_status": result.status,
                }
            )
        )
        await telemetry.shutdown()


def test_missing_tracing_extra_releases_constructor_credentials(tmp_path):
    import inspect
    import shutil
    from pathlib import Path

    import typing_extensions

    shutil.copyfile(inspect.getfile(typing_extensions), tmp_path / "typing_extensions.py")
    root = Path(__file__).resolve().parents[3]
    script = """
import sys
sys.path[:0] = PATHS
from agents import telemetry
try:
 telemetry.OpenAITraceExporter(api_key='synthetic-private-missing-extra')
except ImportError as error:
 assert str(error) == 'Install openai-agents[tracing] to use OpenAITraceExporter'
 tb = error.__traceback__
 while tb:
  if tb.tb_frame.f_globals.get('__name__') == '_agents_codex._trace_export':
   assert 'synthetic-private-missing-extra' not in repr(tb.tb_frame.f_locals)
  tb=tb.tb_next
else:
 raise AssertionError('Missing tracing extra accepted')
""".replace("PATHS", repr([str(root / "src"), str(root / "packages/codex/src"), str(tmp_path)]))
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script], text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_invalid_batch_serialization_does_not_retain_credential_headers():
    secret = "synthetic-invalid-batch-private-key"
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key=secret, endpoint=endpoint)
        event = completed_event()
        event["attributes"] = {"invalid_json": object()}
        try:
            await exporter.on_trace_events((event,))
        except RuntimeError as error:
            assert_sanitized_failure(error, secret)
        else:
            pytest.fail("invalid batch was accepted")
        assert not received


@pytest.mark.parametrize(
    "options",
    [
        {"endpoint": "https://user:SYNTHETIC_PRIVATE_CONFIG@[invalid"},
        {"endpoint": "https://user:SYNTHETIC_PRIVATE_CONFIG@localhost:bad-port"},
        {"organization": "SYNTHETIC_PRIVATE_CONFIG", "project": "bad\nproject"},
        {"project": "SYNTHETIC_PRIVATE_CONFIG", "api_key": "bad\nkey"},
        {"endpoint": "http://localhost/?private=SYNTHETIC_PRIVATE_CONFIG", "api_key": "bad\nkey"},
    ],
)
def test_constructor_validation_releases_library_owned_private_configuration(options):
    secret = "SYNTHETIC_PRIVATE_CONFIG"
    try:
        telemetry.OpenAITraceExporter(**{"api_key": "synthetic-key", **options})
    except ValueError as error:
        assert_sanitized_failure(error, secret)
        assert str(error) in {
            "Invalid trace export endpoint",
            "Trace export headers must be nonempty printable ASCII strings",
        }
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_globals.get("__name__") == "_agents_codex._trace_export":
                # A failed constructor's partial instance belongs to the library.
                # Caller-frame arguments remain caller-owned and are excluded.
                partial = frame.f_locals.get("self")
                if partial is not None:
                    assert secret not in repr(vars(partial))
            traceback = traceback.tb_next
    else:
        pytest.fail("invalid constructor accepted")


@pytest.mark.parametrize("rejected_child", [False, True])
async def test_failed_batch_accounts_for_later_completions(rejected_child):
    roots = [
        {
            **completed_event(),
            "lifecycle": "start",
            "trace_id": f"{index:032x}",
            "name": f"original-root-{index}",
            "attributes": {"sdk.group_id": "original-group"},
        }
        for index in range(1024)
    ]
    overflow = {**roots[0], "trace_id": "f" * 32, "name": "overflow-root"}
    rejected = {**overflow, "parent_span_id": "c" * 16} if rejected_child else overflow
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await exporter.on_trace_events(roots)
        with pytest.raises(RuntimeError):
            await exporter.on_trace_events([rejected, {**roots[0], "lifecycle": "end"}, overflow])
        assert len(received) == 2  # Valid traces survive unrelated admission errors.
        admitted = received[-1]["payload"]["data"]
        assert any(row.get("workflow_name") == "overflow-root" for row in admitted)
        assert any(
            row.get("id") == "span_" + roots[0]["span_id"].rjust(24, "0") for row in admitted
        )
        await exporter.on_trace_event({**overflow, "lifecycle": "end", "name": "renamed-end"})
        assert received[-1]["payload"]["data"][0]["trace_id"] == "trace_" + overflow["trace_id"]
        exporter.set_api_key("synthetic-rotated")
        await exporter.on_trace_event({**roots[1], "lifecycle": "end"})
        descriptor = received[-1]["payload"]["data"][0]
        assert descriptor["workflow_name"] == "original-root-1"
        assert descriptor["group_id"] == "original-group"
        assert len(exporter._traces) == 1024


@pytest.mark.parametrize("delivery_status", [200, 503])
async def test_mixed_loss_batch_delivers_only_admitted_records(delivery_status):
    secret = "synthetic-private-mixed-batch"
    old = {**completed_event(), "lifecycle": "start", "name": "old-root", "sequence": 1}
    fresh = {
        **old,
        "trace_id": "e" * 32,
        "sequence": 3,
        "name": "fresh-root",
        "attributes": {"sdk.metadata.private": secret},
    }
    stale = {
        **old,
        "lifecycle": "end",
        "parent_span_id": "c" * 16,
        "name": "stale-child",
        "sequence": 2,
    }
    with collector([200, delivery_status, 200]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await exporter.on_trace_event(old)
        exporter._on_native_trace_loss(1)
        with pytest.raises(RuntimeError, match="record admission failed") as failure:
            await exporter.on_trace_events(
                [stale, fresh, {**fresh, "lifecycle": "end", "sequence": 4}]
            )
        assert ("delivery failed" in str(failure.value)) is (delivery_status == 503)
        assert_sanitized_failure(failure.value, secret)
        records = received[-1]["payload"]["data"]
        assert len(records) == 2
        assert records[0]["workflow_name"] == "fresh-root"
        assert records[0]["metadata"]["private"] == secret
        assert records[1]["span_data"]["name"] == "fresh-root"
        assert all(
            row.get("trace_id", row.get("id")) == "trace_" + fresh["trace_id"] for row in records
        )
        await exporter.on_trace_event({**fresh, "lifecycle": "end"})
        descriptors = [row for row in received[-1]["payload"]["data"] if row["object"] == "trace"]
        assert len(descriptors) == (1 if delivery_status == 503 else 0)


async def test_original_snapshot_auth_groups_and_retired_forwarding():
    from threading import Event
    from types import MappingProxyType

    from _agents_codex._trace_delivery import _trace_delivery_context, _TraceDeliveryOwner

    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="fallback-key", organization="fallback-org", endpoint=endpoint
        )
        a = MappingProxyType({**completed_event(), "credential_required": True, "sequence": 2})
        b = MappingProxyType(
            {**completed_event(), "credential_required": True, "sequence": 3, "span_id": "c" * 16}
        )
        routes = {
            "2": {
                "identity": "native-A",
                "headers": {"authorization": "Bearer scope-A"},
                "processor_ids": ["host"],
            },
            "3": {
                "identity": "native-B",
                "headers": {"authorization": "Bearer scope-B"},
                "processor_ids": ["host"],
            },
        }
        owner = _TraceDeliveryOwner((a, b), routes, Event(), "host")
        with _trace_delivery_context(owner, (a, b)):
            with pytest.raises(RuntimeError):
                await exporter.on_trace_event({**a, "name": "copied foreign root"})
            await exporter.on_trace_events((a, b))
        owner.retire()
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event(a)
        owner = _TraceDeliveryOwner((a,), routes, Event(), "host")
        with _trace_delivery_context(owner, (a,)):
            await exporter.on_trace_event(a)
        owner.retire()
        await exporter.on_trace_event({**completed_event(), "trace_id": "d" * 32, "sequence": 4})
        headers = [
            {key.lower(): value for key, value in item["headers"].items()} for item in received
        ]
        assert [item["authorization"] for item in headers] == [
            "Bearer scope-A",
            "Bearer scope-B",
            "Bearer scope-A",
            "Bearer fallback-key",
        ]
        assert all(item["openai-organization"] == "fallback-org" for item in headers)
        assert [
            sum(record["object"] == "trace" for record in item["payload"]["data"])
            for item in received
        ] == [1, 1, 1, 1]
        assert received[0]["payload"]["data"][0]["workflow_name"] == completed_event()["name"]
        public = json.dumps([item["payload"] for item in received])
        assert not any(
            value in public
            for value in (
                "scope-A",
                "scope-B",
                "fallback-key",
                "credential_required",
                "native-A",
                "native-B",
            )
        )


async def test_unselected_processor_fallback_requires_original_active_snapshot():
    from threading import Event
    from types import MappingProxyType

    from _agents_codex._trace_delivery import _trace_delivery_context, _TraceDeliveryOwner

    event = MappingProxyType({**completed_event(), "credential_required": True, "sequence": 2})
    routes = {
        "2": {
            "identity": "original-scope",
            "headers": {"authorization": "Bearer original-secret"},
            "processor_ids": ["original"],
        }
    }
    owner = _TraceDeliveryOwner((event,), routes, Event(), "later")
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="later-fallback", endpoint=endpoint)
        with _trace_delivery_context(owner, (event,)):
            with pytest.raises(RuntimeError):
                await exporter.on_trace_event(dict(event))
            await exporter.on_trace_event(event)
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event(event)
        owner.retire()
        with _trace_delivery_context(owner, (event,)), pytest.raises(RuntimeError):
            await exporter.on_trace_event(event)
        assert len(received) == 1
        assert {key.lower(): value for key, value in received[0]["headers"].items()}[
            "authorization"
        ] == "Bearer later-fallback"
        assert "original-secret" not in json.dumps(received)


async def native_auth_case():
    from collections.abc import Mapping

    observed = []
    groups = []
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="fallback-secret", organization="fallback-org", endpoint=endpoint
        )

        class Forwarder:
            async def on_trace_event(self, event):
                await self.on_trace_events((event,))

            async def on_trace_events(self, events):
                observed.extend(events)
                groups.append(events)
                scoped = next(
                    (event for event in events if event.get("credential_required") is True), None
                )
                if scoped is not None:
                    with pytest.raises(RuntimeError):
                        await exporter.on_trace_event({**scoped, "name": "copied foreign root"})
                await exporter.on_trace_events(events)

        await telemetry.configure(processors=[Forwarder()])
        a = telemetry.TraceExportCredentials(headers={"Authorization": "Bearer trace-secret-A"})
        with telemetry.trace("authenticated-A", options={"export_credentials": a}) as root_a:
            pass
        del a
        b = telemetry.TraceExportCredentials(headers={"Authorization": "Bearer trace-secret-B"})
        with telemetry.trace("authenticated-B", options={"export_credentials": b}) as root_b:
            pass
        with telemetry.span(
            "override-B-under-A", options={"parent": root_a.context, "export_credentials": b}
        ):
            pass
        del b
        with telemetry.span("late-child-A", options={"parent": root_a.context}):
            pass
        with telemetry.span("late-child-B", options={"parent": root_b.context}):
            pass
        with telemetry.trace("fallback"):
            pass
        result = await telemetry.force_flush()
        assert result.status == "success"
        expected = {
            "Bearer trace-secret-A": {"authenticated-A", "late-child-A"},
            "Bearer trace-secret-B": {"authenticated-B", "late-child-B", "override-B-under-A"},
            "Bearer fallback-secret": {"fallback"},
        }
        for auth, names in expected.items():
            requests = [
                item
                for item in received
                if {key.lower(): value for key, value in item["headers"].items()}["authorization"]
                == auth
            ]
            assert requests
            assert all(
                {key.lower(): value for key, value in item["headers"].items()}[
                    "openai-organization"
                ]
                == "fallback-org"
                for item in requests
            )
            records = [record for item in requests for record in item["payload"]["data"]]
            assert {
                record["span_data"]["name"]
                for record in records
                if record["object"] == "trace.span"
            } == names
            assert any(record["object"] == "trace" for record in records)
        assert any(len({event["trace_id"] for event in events}) >= 2 for events in groups)

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(item) for item in value]
            return value

        public = json.dumps(plain(observed))
        assert not any(
            secret in public
            for secret in (
                "trace-secret-A",
                "trace-secret-B",
                "fallback-secret",
                "Authorization",
                "authorization",
            )
        )
        count = len(received)
        scoped = next(event for event in observed if event.get("credential_required") is True)
        with pytest.raises(RuntimeError):
            await exporter.on_trace_event(scoped)
        assert len(received) == count
        await telemetry.shutdown()


async def native_auth_destination_case():
    with collector() as (otlp_endpoint, otlp_received), collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="host-fallback", endpoint=endpoint)
        await telemetry.configure(
            traces=telemetry.OtlpHttp(endpoint=otlp_endpoint, protocol="json"),
            processors=[exporter],
        )
        try:
            credentials = telemetry.TraceExportCredentials(
                headers={"Authorization": "Bearer selected-otlp"}
            )
            with telemetry.trace(
                "selected-destination", options={"export_credentials": credentials}
            ):
                pass
            assert (await telemetry.force_flush()).status == "success"
            assert any(
                {key.lower(): value for key, value in item["headers"].items()}.get("authorization")
                == "Bearer selected-otlp"
                for item in otlp_received
            )
            assert received
            assert {
                {key.lower(): value for key, value in item["headers"].items()}["authorization"]
                for item in received
            } == {"Bearer host-fallback"}
        finally:
            await telemetry.shutdown()


async def native_auth_replacement_case():
    with collector() as (old_endpoint, old_received), collector() as (endpoint, received):
        original = telemetry.OpenAITraceExporter(api_key="original-fallback", endpoint=old_endpoint)
        replacement = telemetry.OpenAITraceExporter(
            api_key="replacement-fallback", endpoint=endpoint
        )
        await telemetry.configure(processors=[original])
        try:
            credentials = telemetry.TraceExportCredentials(
                headers={"Authorization": "Bearer original-scope"}
            )
            with telemetry.trace(
                "original-owner", options={"export_credentials": credentials}
            ) as root:
                pass
            assert (await telemetry.force_flush()).status == "success"
            telemetry.set_trace_processors([replacement])
            with telemetry.span("inherited-after-replacement", options={"parent": root.context}):
                pass
            assert (await telemetry.force_flush()).status == "success"
            assert old_received
            assert {
                {key.lower(): value for key, value in item["headers"].items()}["authorization"]
                for item in old_received
            } == {"Bearer original-scope"}
            assert received
            assert {
                {key.lower(): value for key, value in item["headers"].items()}["authorization"]
                for item in received
            } == {"Bearer replacement-fallback"}
        finally:
            await telemetry.shutdown()
