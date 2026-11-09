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
from threading import Thread
from typing import Any

import pytest

from agents import Agent, telemetry
from agents.testing import ScriptedResponses, text


@contextmanager
def collector(statuses: list[int] | None = None) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received = []
    replies = list(statuses or [])

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
            self.send_response(replies.pop(0) if replies else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces/ingest", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def completed_event():
    return {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "parent_span_id": "c" * 16,
        "name": "synthetic operation",
        "lifecycle": "end",
        "outcome": "error",
        "start_time_unix_nanos": "1700000000123456789",
        "end_time_unix_nanos": "1700000001987654321",
        "attributes": {"sdk.outcome.category": "synthetic_error"},
    }


def test_exporter_retries_bounded_http_failure_and_preserves_wire_identity():
    with collector([503, 200]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-key", endpoint=endpoint, max_retries=1
        )
        exporter.on_trace_event(completed_event())
    assert len(received) == 2
    assert received[-1]["headers"]["Authorization"] == "Bearer synthetic-key"
    assert received[-1]["headers"]["OpenAI-Beta"] == "traces=v1"
    trace, span = received[-1]["payload"]["data"]
    assert trace["id"] == "trace_" + "a" * 32
    assert span["id"] == "span_" + "0" * 8 + "b" * 16
    assert span["parent_id"] == "span_" + "0" * 8 + "c" * 16
    assert span["started_at"] == "2023-11-14T22:13:20.123456789Z"
    assert span["ended_at"] == "2023-11-14T22:13:21.987654321Z"
    assert span["error"]["data"]["outcome"] == "error"


@pytest.mark.parametrize("status", [302, 403, 429])
def test_exporter_rejects_redirects_and_terminal_failure_without_secret_diagnostics(status):
    with collector([status]) as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(
            api_key="synthetic-private-key", endpoint=endpoint, max_retries=3
        )
        with pytest.raises(RuntimeError) as failure:
            exporter.on_trace_event(completed_event())
        assert str(failure.value) == "Native OpenAI trace export failed"
        assert failure.value.__cause__ is None
        assert len(received) == 1


def test_real_native_capture_policy_and_openai_export():
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; "
            "from tests.test_native_trace_exporter import native_case; asyncio.run(native_case())",
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
        with telemetry.trace("allowed", options={"include_sensitive_data": True}):
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
        assert (await telemetry.force_flush()).status == "success"
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
