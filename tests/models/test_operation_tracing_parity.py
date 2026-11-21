"""Compare real provider retries and SDK tool boundaries against a loopback endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, Literal

import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider
from agents.models.openai_provider import OpenAIProvider
from agents.tracing import ResponseSpanData, set_trace_processors

from ..testing_processor import SPAN_PROCESSOR_TESTING, SpanProcessorForTests

pytestmark = pytest.mark.allow_call_model_methods
MODEL = "fixture-model"


class _LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Numeric loopback avoids reverse DNS and never reaches a model service.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _ResponsesFixture:
    def __init__(self, *, native_path: Path | None = None) -> None:
        self.native = native_path is not None
        self.native_path = native_path
        self.requests: list[dict[str, Any]] = []
        self.intervals: list[tuple[int, int]] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, body: bytes, content_type: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("openai-model", MODEL)
                if status == 500:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self.reply(b'{"data":[]}', "application/json")

            def do_POST(self) -> None:
                started = time.time_ns()
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(request)
                number = len(fixture.requests)
                if number == 1:
                    fixture.intervals.append((started, time.time_ns()))
                    if fixture.native:
                        # SSE EOF after a successful handshake exercises Core's stream retry.
                        self.reply(
                            b"event: response.created\ndata: "
                            + json.dumps(
                                {"type": "response.created", "response": {"id": "failed-stream"}}
                            ).encode()
                            + b"\n\n",
                            "text/event-stream",
                        )
                    else:
                        self.reply(
                            b'{"error":{"message":"Synthetic retry","type":"server_error"}}',
                            "application/json",
                            500,
                        )
                    return
                item = (
                    {
                        "type": "function_call",
                        "id": f"fc_{number}",
                        "call_id": f"call_{number}",
                        "name": "view_image" if fixture.native and number == 2 else "lookup",
                        "arguments": json.dumps({"path": str(fixture.native_path)})
                        if fixture.native and number == 2
                        else '{"key":"argument-canary"}',
                        "status": "completed",
                    }
                    if number <= 2 + fixture.native
                    else {
                        "type": "message",
                        "id": "msg_answer",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "answer-canary", "annotations": []}
                        ],
                    }
                )
                response = {
                    "id": f"response_{number}",
                    "object": "response",
                    "created_at": 1,
                    "model": MODEL,
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                }
                if request.get("stream"):
                    events = [
                        {
                            "type": "response.created",
                            "response": dict(response, output=[], status="in_progress"),
                        },
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": dict(item, status="in_progress"),
                        },
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": response},
                    ]
                    body = "".join(
                        f"event: {event['type']}\ndata: "
                        + json.dumps(dict(event, sequence_number=index))
                        + "\n\n"
                        for index, event in enumerate(events)
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    body, content_type = json.dumps(response).encode(), "application/json"
                fixture.intervals.append((started, time.time_ns()))
                self.reply(body, content_type)

        self.server = _LoopbackServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


class _Callbacks(SpanProcessorForTests):
    def __init__(self) -> None:
        super().__init__()
        self.starts: Counter[str] = Counter()
        self.ends: Counter[str] = Counter()

    def on_span_start(self, span: Any) -> None:
        super().on_span_start(span)
        self.starts[span.span_id] += 1

    def on_span_end(self, span: Any) -> None:
        super().on_span_end(span)
        self.ends[span.span_id] += 1


def _ns(value: str | None) -> int:
    assert value is not None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def _provider(name: str, tmp_path: Path, server: _ResponsesFixture) -> Any:
    if name == "openai":
        return OpenAIProvider(
            api_key="loopback-placeholder", base_url=server.base_url, use_responses=True
        )
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text(f'''model = "{MODEL}"
model_provider = "fixture"
approval_policy = "never"
sandbox_mode = "read-only"
[analytics]
enabled = false
[features]
apps = false
hooks = false
plugins = false
[model_providers.fixture]
name = "Synthetic loopback"
base_url = "{server.base_url}"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[otel]
metrics_exporter = "none"
exporter = "none"
trace_exporter = "none"
''')
    return CodexProvider(cwd=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["openai", "codex", "codex-native"])
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "stream"])
@pytest.mark.parametrize(
    "detail", [None, "metadata", "content"], ids=["default", "metadata", "content"]
)
async def test_logical_model_retry_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    streamed: bool,
    detail: Literal["metadata", "content"] | None,
) -> None:
    await _exercise_retry_parity(tmp_path, monkeypatch, provider_name, streamed, detail)


async def _exercise_retry_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    streamed: bool,
    detail: Literal["metadata", "content"] | None,
) -> None:
    native = provider_name == "codex-native"
    if provider_name != "openai" and os.environ.get("CODEX_TRACING_NATIVE_TEST") != "1":
        pytest.skip("Set CODEX_TRACING_NATIVE_TEST=1 and use an installed SDK wheel.")
    if os.environ.get("CODEX_TRACING_NATIVE_TEST") == "1":
        import agents

        assert (
            not Path(agents.__file__)
            .resolve()
            .is_relative_to(Path(__file__).resolve().parents[2] / "src")
        ), "Run with the installed wheel and pytest -o pythonpath=."
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agents.extensions.tracing import OpenTelemetryTracingProcessor

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    callbacks = _Callbacks()
    set_trace_processors(
        [callbacks, OpenTelemetryTracingProcessor(tracer_provider=tracer_provider)]
    )
    image = tmp_path / "pixel.png" if native else None
    if image is not None:
        image.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
            )
        )
    server = _ResponsesFixture(native_path=image)
    provider = _provider(provider_name, tmp_path, server)
    tool_intervals: list[tuple[int, int]] = []

    @tool
    async def lookup(key: str) -> str:
        """Look up one synthetic record."""
        started = time.time_ns()
        assert key == "argument-canary"
        tool_intervals.append((started, time.time_ns()))
        return "result-canary"

    async def exercise() -> Any:
        agent = Agent(
            name="worker",
            model=MODEL,
            instructions="Use the supplied tool once.",
            tools=[lookup],
        )
        config = RunConfig(
            model_provider=provider, tracing={} if detail is None else {"operation_detail": detail}
        )
        if not streamed:
            return await Runner.run(agent, "prompt-canary", run_config=config)
        result = Runner.run_streamed(agent, "prompt-canary", run_config=config)
        async for _ in result.stream_events():
            pass
        return result

    try:
        result = await asyncio.wait_for(exercise(), timeout=30)
        assert result.final_output == "answer-canary"
        assert len(server.requests) == 3 + native
        assert server.requests[0]["input"] == server.requests[1]["input"]
        assert server.requests[1]["input"] != server.requests[2]["input"]
        submitted = json.dumps(server.requests[2]["input"])
        assert ("input_image" if native else "result-canary") in submitted
        assert "result-canary" in json.dumps(server.requests[-1]["input"])
        assert len(tool_intervals) == 1
        models = [
            span
            for span in callbacks.get_ordered_spans()
            if span.span_data.type in {"response", "generation"}
        ]
        assert len(models) == 2 + native
        for span in models:
            assert span.span_data.usage["input_tokens"] == 10
            assert span.span_data.usage["output_tokens"] == 5
        assert all(isinstance(span.span_data, ResponseSpanData) for span in models)
        for span in models:
            assert callbacks.starts[span.span_id] == callbacks.ends[span.span_id] == 1
            assert span.error is None
            assert span.span_data.operation.outcome == "completed"
        if detail is None:
            assert all(span.span_data.output is None for span in models)
            assert "prompt-canary" in repr(models[0].span_data.input)
            assert "prompt-canary" in repr(models[-1].span_data.input)
            if native:
                assert models[0].span_data.response is None
                assert models[1].span_data.input is None
            assert all(span.span_data.response is not None for span in models[native:])
        else:
            assert all(span.span_data.response is None for span in models)
            if detail == "metadata":
                assert all(
                    span.span_data.input is None and span.span_data.output is None
                    for span in models
                )
            else:
                assert "prompt-canary" in repr(models[0].span_data.input)
                assert "result-canary" in repr(models[-1].span_data.input)
                assert "answer-canary" in repr(models[-1].span_data.output)
                if native:
                    captured = repr(models[1].span_data.input)
                    assert "omitted" in captured
                    assert "data:image/" not in captured
                    assert "iVBORw0KGgo" not in captured
        assert _ns(models[0].started_at) <= server.intervals[0][0] + 1_000_000
        assert _ns(models[0].ended_at) >= server.intervals[1][1] - 1_000_000
        assert _ns(models[-2].ended_at) <= tool_intervals[0][0]
        assert tool_intervals[0][1] <= _ns(models[-1].started_at) + 1_000_000
        assert _ns(models[-1].ended_at) >= server.intervals[-1][1] - 1_000_000
        tools = [
            span for span in callbacks.get_ordered_spans() if span.span_data.type == "function"
        ]
        assert [span.span_data.name for span in tools] == (["view_image"] if native else []) + [
            "lookup"
        ]
        assert all(
            callbacks.starts[span.span_id] == callbacks.ends[span.span_id] == 1 for span in tools
        )
        if native:
            # Native tools may start before the preceding response stream finishes.
            assert _ns(models[0].started_at) <= _ns(tools[0].started_at)
            assert _ns(tools[0].ended_at) <= _ns(models[1].started_at)
            assert _ns(models[0].ended_at) <= _ns(models[1].started_at)
            if detail == "content":
                assert json.loads(tools[0].span_data.input)["path"] == str(image)
                assert "omitted" in repr(tools[0].span_data.output)
                assert "iVBORw0KGgo" not in repr(tools[0].span_data.output)
            else:
                assert tools[0].span_data.input is None
                assert tools[0].span_data.output is None
        retries = models[0].span_data.operation.retry_timestamps
        assert len(retries) == 1
        assert all(span.span_data.operation.retry_timestamps == [] for span in models[1:])
        assert _ns(models[0].started_at) <= _ns(retries[0]) <= _ns(models[0].ended_at)
        chats = [
            span
            for span in exporter.get_finished_spans()
            if (span.attributes or {}).get("gen_ai.operation.name") == "chat"
        ]
        chats.sort(key=lambda span: span.start_time)
        assert len(chats) == 2 + native
        exported = json.dumps([dict(span.attributes or {}) for span in chats])
        for canary in ("prompt-canary", "result-canary", "answer-canary"):
            assert (canary in exported) == (detail == "content")
        retry_events = [
            event for event in chats[0].events if event.name == "openai.agents.model.retry"
        ]
        assert len(retry_events) == 1
        assert retry_events[0].attributes is not None
        assert retry_events[0].attributes["openai.agents.model.attempt"] == 2
        assert chats[0].start_time is not None and chats[0].end_time is not None
        assert chats[0].start_time <= retry_events[0].timestamp <= chats[0].end_time
        assert abs(retry_events[0].timestamp - _ns(retries[0])) <= 1_000
        assert not [
            event
            for span in chats[1:]
            for event in span.events
            if event.name == "openai.agents.model.retry"
        ]
        if native and detail == "content":
            assert "omitted" in exported
            assert "data:image/" not in exported
            assert "iVBORw0KGgo" not in exported
    finally:
        try:
            await provider.aclose()
        finally:
            server.close()
            set_trace_processors([SPAN_PROCESSOR_TESTING])
            tracer_provider.shutdown()
