"""Exercise provider-wire conversion and actual subprocess tool ownership over loopback."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider
from agents.models.interface import ModelProvider
from agents.models.openai_provider import OpenAIProvider
from agents.tracing import TracingConfig, set_trace_processors

from ..testing_processor import SPAN_PROCESSOR_TESTING, SpanProcessorForTests

pytestmark = pytest.mark.allow_call_model_methods

MODEL = "fixture-model"
PROMPT = "parity_prompt_canary"
INSTRUCTIONS = "Answer using the supplied tools. parity_instructions_canary"
ARGUMENT = "parity_argument_canary"
TOOL_RESULT = "parity_tool_result_canary"
ANSWER = "parity_answer_canary"
FILE_CONTENT = "parity_native_file_canary"
PAYLOADS = (PROMPT, INSTRUCTIONS, ARGUMENT, TOOL_RESULT, ANSWER)


class _ResponsesFixture:
    def __init__(
        self,
        *,
        mixed: bool = False,
        model_header: bool = True,
        held: bool = False,
        retry_once: bool = False,
        observed_model: str = MODEL,
    ) -> None:
        self.mixed = mixed
        self.model_header = model_header
        self.held = held
        self.retry_once = retry_once
        self.observed_model = observed_model
        self.request_started = threading.Event()
        self.release = threading.Event()
        self.requests: list[dict[str, Any]] = []
        self.intervals: list[tuple[int, int]] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._reply(b'{"data":[]}', "application/json")

            def _reply(self, body: bytes, content_type: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if status == 500:
                    self.send_header("Retry-After", "0")
                if fixture.model_header:
                    self.send_header("openai-model", fixture.observed_model)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self) -> None:
                started = time.time_ns()
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(request)
                fixture.request_started.set()
                if fixture.held:
                    fixture.release.wait(timeout=10)
                number = len(fixture.requests)
                if fixture.retry_once and number == 1:
                    fixture.intervals.append((started, time.time_ns()))
                    self._reply(
                        b'{"error":{"message":"Fixture retry","type":"server_error"}}',
                        "application/json",
                        500,
                    )
                    return
                if fixture.retry_once:
                    number -= 1
                response = fixture.response(number)
                if request.get("stream"):
                    item = response["output"][0]
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
                    body = json.dumps(response).encode()
                    content_type = "application/json"
                fixture.intervals.append((started, time.time_ns()))
                self._reply(body, content_type)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def response(self, number: int) -> dict[str, Any]:
        if self.mixed and number == 1:
            item: dict[str, Any] = {
                "type": "function_call",
                "name": "read_file",
                "arguments": "{}",
                "call_id": "native_read",
                "id": "fc_native_read",
                "status": "completed",
            }
            if any(
                tool.get("type") == "namespace" and tool.get("name") == "mcp__parity"
                for tool in self.requests[0].get("tools", [])
            ):
                item["namespace"] = "mcp__parity"
            else:
                item["name"] = "mcp__parity__read_file"
        elif number == (2 if self.mixed else 1) and not self.retry_once:
            item = {
                "type": "function_call",
                "name": "lookup",
                "arguments": json.dumps({"key": ARGUMENT}),
                "call_id": "python_lookup",
                "id": "fc_python_lookup",
                "status": "completed",
            }
        else:
            item = {
                "type": "message",
                "id": "msg_answer",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
            }
        return {
            "id": f"resp_parity_{number}",
            "object": "response",
            "created_at": 1,
            "model": self.observed_model,
            "status": "completed",
            "output": [item],
            "usage": {
                "input_tokens": 10 * number,
                "output_tokens": 5 * number,
                "total_tokens": 15 * number,
                "input_tokens_details": {"cached_tokens": number},
                "output_tokens_details": {"reasoning_tokens": number},
            },
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


class _LiveProcessor(SpanProcessorForTests):
    def __init__(self) -> None:
        super().__init__()
        self.started_models: list[Any] = []
        self.model_started = asyncio.Event()

    def on_span_start(self, span: Any) -> None:
        super().on_span_start(span)
        if span.span_data.type in {"response", "generation"}:
            self.started_models.append(span)
            self.model_started.set()


@pytest.fixture
def telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Any, Any, SpanProcessorForTests, Any]]:
    monkeypatch.delenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", raising=False)
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agents.extensions.tracing import OpenTelemetryTracingProcessor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    native = _LiveProcessor()
    otel_processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([native, otel_processor])
    try:
        yield provider.get_tracer("operation-parity"), exporter, native, otel_processor
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


def _codex_provider(
    tmp_path: Path, server: _ResponsesFixture, *, pid_file: Path | None = None
) -> CodexProvider:
    executable = os.environ.get("CODEX_TRACING_TEST_EXECUTABLE")
    if not executable:
        pytest.skip("Set CODEX_TRACING_TEST_EXECUTABLE to run patched Codex integration tests.")
    assert Path(executable).is_file(), "CODEX_TRACING_TEST_EXECUTABLE must name a real binary."
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = f'''model = "{MODEL}"
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
name = "Synthetic fixture"
base_url = "{server.base_url}"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[otel]
metrics_exporter = "none"
exporter = "none"
trace_exporter = "none"
'''
    if server.mixed:
        mcp_server = tmp_path / "parity_mcp.py"
        mcp_server.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "try:\n"
            "    from mcp.server.mcpserver import MCPServer\n"
            "except ImportError:\n"
            "    from mcp.server.fastmcp import FastMCP as MCPServer\n"
            "from mcp.types import ToolAnnotations\n"
            'server = MCPServer("parity")\n'
            "@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))\n"
            "def read_file() -> str:\n"
            '    """Read the synthetic fixture file."""\n'
            "    return Path(sys.argv[1]).read_text()\n"
            'server.run(transport="stdio")\n'
        )
        config += (
            "\n[mcp_servers.parity]\ncommand = "
            + json.dumps(sys.executable)
            + "\nargs = "
            + json.dumps([str(mcp_server), str(tmp_path / "parity-native.txt")])
            + "\nstartup_timeout_sec = 10\ntool_timeout_sec = 5\n"
            + 'default_tools_approval_mode = "approve"\n'
        )
    (home / "config.toml").write_text(config)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "TMPDIR"}
    }
    environment["CODEX_HOME"] = str(home)
    if pid_file is not None:
        # The launcher records the real child PID and immediately executes the unmodified CLI.
        launcher = tmp_path / "tracked-codex"
        launcher.write_text(
            "#!/bin/sh\nprintf '%s' \"$$\" > "
            + shlex.quote(str(pid_file))
            + "\nexec "
            + shlex.quote(str(Path(executable).resolve()))
            + ' "$@"\n'
        )
        launcher.chmod(0o700)
        executable = str(launcher)
    return CodexProvider(cwd=tmp_path, codex_executable=executable, env=environment)


def _ns(timestamp: str | None) -> int:
    assert timestamp is not None
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def _model_provider(name: str, tmp_path: Path, server: _ResponsesFixture) -> ModelProvider:
    if name == "codex":
        return _codex_provider(tmp_path, server)
    return OpenAIProvider(
        api_key="loopback-test-placeholder", base_url=server.base_url, use_responses=True
    )


async def _exercise(
    *,
    tmp_path: Path,
    telemetry: tuple[Any, Any, SpanProcessorForTests, Any],
    provider_name: str,
    streamed: bool,
    capture: str,
    mixed: bool = False,
    consumer: str = "both",
    omit_task_spans: bool = False,
    model_header: bool = True,
    observed_model: str = MODEL,
) -> None:
    tracer, exporter, native, otel_processor = telemetry
    set_trace_processors(
        [native]
        if consumer == "sdk"
        else [otel_processor]
        if consumer == "otel"
        else [native, otel_processor]
    )
    server = _ResponsesFixture(
        mixed=mixed,
        model_header=model_header,
        observed_model=observed_model,
    )
    callback_intervals: list[tuple[int, int]] = []
    model_provider = None
    try:
        (tmp_path / "parity-native.txt").write_text(FILE_CONTENT)
        model_provider = _model_provider(provider_name, tmp_path, server)

        @tool
        async def lookup(key: str) -> str:
            assert key == ARGUMENT
            started = time.time_ns()
            with tracer.start_as_current_span("database.query"):
                pass
            callback_intervals.append((started, time.time_ns()))
            return TOOL_RESULT

        agent = Agent(name="worker", model=MODEL, instructions=INSTRUCTIONS, tools=[lookup])
        tracing: TracingConfig = {}
        if omit_task_spans:
            tracing["include_task_and_turn_spans"] = False
        if capture in {"metadata", "content", "veto"}:
            tracing["operation_detail"] = "content" if capture == "veto" else capture
        config = RunConfig(
            model_provider=model_provider,
            workflow_name="parity-workflow",
            tracing=tracing,
            tracing_disabled=capture == "disabled",
        )
        if capture in {"veto", "default-veto"}:
            config.trace_include_sensitive_data = False
        with tracer.start_as_current_span("host_job") as host:
            if streamed:
                result = Runner.run_streamed(agent, PROMPT, run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                result = await Runner.run(agent, PROMPT, run_config=config)
        assert result.final_output == ANSWER
        assert len(server.requests) == (3 if mixed else 2)
        assert all(request["model"] == MODEL for request in server.requests)
        assert len(callback_intervals) == 1
        if mixed:
            native_results = [
                item.get("output")
                for item in server.requests[1]["input"]
                if item.get("call_id") == "native_read" and "output" in item
            ]
            assert FILE_CONTENT in json.dumps(native_results), {
                "result": native_results,
                "mcp_tools": [
                    tool
                    for tool in server.requests[0].get("tools", [])
                    if "parity" in json.dumps(tool)
                ],
            }
        spans = native.get_ordered_spans()
        otel = exporter.get_finished_spans()
        database = next(span for span in otel if span.name == "database.query")
        if capture == "disabled":
            assert not spans
            assert len(otel) == 2
            assert database.parent.span_id == host.get_span_context().span_id
            return

        models = [span for span in spans if span.span_data.type in {"response", "generation"}]
        tools = [span for span in spans if span.span_data.type == "function"]
        turns = [span for span in spans if span.span_data.type == "turn"]
        if consumer != "otel":
            assert len(models) == len(server.requests)
            assert len(tools) == (2 if mixed else 1)
            assert len(turns) == (0 if omit_task_spans else 2)
            assert {span.span_data.type for span in models} == {
                "generation" if capture in {"metadata", "content", "veto"} else "response"
            }
            native_index = {span.span_id: span for span in spans}
            assert all(
                native_index[span.parent_id].span_data.type
                == ("agent" if omit_task_spans else "turn")
                for span in models + tools
            )
            assert all(span.error is None for span in models + tools)
            for index, span in enumerate(models, start=1):
                assert span.span_data.operation.model == MODEL
                assert span.span_data.operation.response_model == observed_model
                assert span.span_data.operation.outcome == "completed"
                if capture in {"metadata", "veto", "default-veto"}:
                    assert span.span_data.operation.response_id is None
                else:
                    assert span.span_data.operation.response_id == f"resp_parity_{index}"
                assert span.span_data.usage["input_tokens"] == 10 * index
                assert span.span_data.usage["output_tokens"] == 5 * index
                started, ended = server.intervals[index - 1]
                assert _ns(span.started_at) <= started + 1_000_000
                assert _ns(span.ended_at) >= ended - 1_000_000
            if not omit_task_spans:
                expected_turn_usage = [(30, 15), (30, 15)] if mixed else [(10, 5), (20, 10)]
                assert [
                    (span.span_data.usage["input_tokens"], span.span_data.usage["output_tokens"])
                    for span in turns
                ] == expected_turn_usage
            if mixed:
                native_tool = next(span for span in tools if span.span_data.name != "lookup")
                assert _ns(models[0].started_at) <= _ns(native_tool.started_at)
                assert _ns(native_tool.ended_at) <= _ns(models[1].started_at)
        operations = [
            span for span in otel if span.attributes.get("gen_ai.operation.name") == "chat"
        ]
        executions = [
            span for span in otel if span.attributes.get("gen_ai.operation.name") == "execute_tool"
        ]
        if consumer == "sdk":
            assert not operations and not executions
            assert database.parent.span_id == host.get_span_context().span_id
            return
        assert len(operations) == len(server.requests)
        assert len(executions) == (2 if mixed else 1)
        otel_index = {span.context.span_id: span for span in otel}
        for span in operations + executions:
            assert span.context.trace_id == host.get_span_context().trace_id
            assert otel_index[span.parent.span_id].attributes["gen_ai.operation.name"] == (
                "invoke_agent" if omit_task_spans else "openai.agents.turn"
            )
        for index, span in enumerate(
            sorted(operations, key=lambda value: value.start_time), start=1
        ):
            assert span.attributes["gen_ai.request.model"] == MODEL
            assert span.attributes["gen_ai.response.model"] == observed_model
            assert span.attributes["gen_ai.usage.input_tokens"] == 10 * index
            assert span.attributes["gen_ai.usage.output_tokens"] == 5 * index
            assert span.attributes["openai.api.type"] == "responses"
            assert span.kind.name == "CLIENT"
            started, ended = server.intervals[index - 1]
            assert span.start_time <= started + 1_000_000
            assert span.end_time >= ended - 1_000_000
        assert otel_index[database.parent.span_id] in executions
        python_execution = otel_index[database.parent.span_id]
        callback_started, callback_ended = callback_intervals[0]
        assert python_execution.start_time <= callback_started
        assert python_execution.end_time >= callback_ended
        assert python_execution.start_time <= database.start_time <= database.end_time
        assert database.end_time <= python_execution.end_time
        if models:
            assert _ns(models[0].ended_at) <= callback_started + 1_000_000
        for span in otel:
            current = span
            while current.parent is not None:
                current = otel_index[current.parent.span_id]
            assert current.context.span_id == host.get_span_context().span_id

        exported = json.dumps(
            [
                {
                    "attributes": dict(span.attributes),
                    "events": [
                        {"name": event.name, "attributes": dict(event.attributes or {})}
                        for event in span.events
                    ],
                    "status": span.status.description,
                }
                for span in otel
            ]
        )
        if capture == "content":
            assert all(marker in exported for marker in PAYLOADS)
            for span, request in zip(models, server.requests, strict=True):
                submitted = {"input": request["input"], "instructions": request.get("instructions")}
                assert len(json.dumps(submitted, ensure_ascii=False).encode()) < 32 * 1024
                assert span.span_data.input == request["input"]
                if isinstance(request.get("instructions"), str):
                    assert span.span_data.model_config["instructions"] == request["instructions"]
        else:
            assert all(marker not in exported for marker in PAYLOADS)
        if capture in {"metadata", "veto", "default-veto"}:
            assert all(span.span_data.input is None for span in models + tools)
            assert all(getattr(span.span_data, "output", None) is None for span in models + tools)
            assert all(getattr(span.span_data, "response", None) is None for span in models)
        if capture == "default" and consumer != "otel":
            assert PROMPT in json.dumps(models[0].span_data.input)
            assert ANSWER in models[-1].span_data.response.model_dump_json()
            python_tool = next(span for span in tools if span.span_data.name == "lookup")
            assert ARGUMENT in python_tool.span_data.input
            assert TOOL_RESULT in str(python_tool.span_data.output)
        if mixed:
            native_payloads = json.dumps([span.export() for span in spans], default=str)
            if capture == "content":
                assert FILE_CONTENT in exported
            else:
                assert FILE_CONTENT not in exported
                assert FILE_CONTENT not in native_payloads
    finally:
        if model_provider is not None:
            await model_provider.aclose()
        server.close()


@pytest.mark.parametrize("provider_name", ["default", "codex"])
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "stream"])
@pytest.mark.parametrize(
    "capture", ["default", "default-veto", "metadata", "content", "veto", "disabled"]
)
async def test_operation_tracing_parity(
    tmp_path: Path, telemetry: Any, provider_name: str, streamed: bool, capture: str
) -> None:
    await asyncio.wait_for(
        _exercise(
            tmp_path=tmp_path,
            telemetry=telemetry,
            provider_name=provider_name,
            streamed=streamed,
            capture=capture,
        ),
        timeout=30,
    )


@pytest.mark.parametrize("streamed", [False, True], ids=["run", "stream"])
@pytest.mark.parametrize("capture", ["default", "metadata", "content", "veto"])
async def test_codex_native_and_python_tool_parity(
    tmp_path: Path, telemetry: Any, streamed: bool, capture: str
) -> None:
    await asyncio.wait_for(
        _exercise(
            tmp_path=tmp_path,
            telemetry=telemetry,
            provider_name="codex",
            streamed=streamed,
            capture=capture,
            mixed=True,
        ),
        timeout=30,
    )


@pytest.mark.parametrize("provider_name", ["default", "codex"])
@pytest.mark.parametrize("consumer", ["sdk", "otel"])
async def test_operation_tracing_individual_consumers(
    tmp_path: Path, telemetry: Any, provider_name: str, consumer: str
) -> None:
    await asyncio.wait_for(
        _exercise(
            tmp_path=tmp_path,
            telemetry=telemetry,
            provider_name=provider_name,
            streamed=False,
            capture="default",
            consumer=consumer,
        ),
        timeout=30,
    )


async def test_operation_tracing_without_task_and_turn_spans(
    tmp_path: Path, telemetry: Any
) -> None:
    await asyncio.wait_for(
        _exercise(
            tmp_path=tmp_path,
            telemetry=telemetry,
            provider_name="default",
            streamed=False,
            capture="metadata",
            omit_task_spans=True,
        ),
        timeout=30,
    )


@pytest.mark.parametrize("provider_name", ["default", "codex"])
async def test_operation_tracing_observed_model_without_header(
    tmp_path: Path, telemetry: Any, provider_name: str
) -> None:
    await asyncio.wait_for(
        _exercise(
            tmp_path=tmp_path,
            telemetry=telemetry,
            provider_name=provider_name,
            streamed=False,
            capture="metadata",
            model_header=False,
            observed_model=f"{MODEL}-observed",
        ),
        timeout=30,
    )


@pytest.mark.parametrize(
    ("provider_name", "termination"),
    [("default", "cancel"), ("codex", "cancel"), ("codex", "child-exit")],
)
async def test_operation_tracing_live_start_and_cancel(
    tmp_path: Path, telemetry: Any, provider_name: str, termination: str
) -> None:
    if termination == "child-exit" and os.name != "posix":
        pytest.skip("The transparent child-PID launcher requires a POSIX shell.")
    tracer, exporter, native, _ = telemetry
    server = _ResponsesFixture(held=True)
    model_provider = None
    task = None
    try:
        pid_file = tmp_path / "codex.pid"
        model_provider = (
            _codex_provider(tmp_path, server, pid_file=pid_file)
            if termination == "child-exit"
            else _model_provider(provider_name, tmp_path, server)
        )
        with tracer.start_as_current_span("host_job") as host:
            task = asyncio.create_task(
                Runner.run(
                    Agent(name="worker", model=MODEL),
                    PROMPT,
                    run_config=RunConfig(
                        model_provider=model_provider, tracing={"operation_detail": "metadata"}
                    ),
                )
            )
            assert await asyncio.to_thread(server.request_started.wait, 5)
            await asyncio.wait_for(native.model_started.wait(), 5)
            assert len(native.started_models) == 1
            assert native.started_models[0].ended_at is None
            if termination == "child-exit":
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
                with pytest.raises(Exception, match="Codex App Server.*(?:exited|closed)"):
                    await asyncio.wait_for(task, 10)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            assert host.get_span_context().is_valid
        models = [
            span for span in native.get_ordered_spans() if span.span_data.type == "generation"
        ]
        assert len(models) == 1
        assert models[0] is native.started_models[0]
        assert models[0].ended_at is not None
        assert models[0].span_data.operation.outcome == "interrupted"
        chats = [
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "chat"
        ]
        assert len(chats) == 1
        assert chats[0].end_time is not None
        if termination == "child-exit":
            assert models[0].span_data.operation.estimated_end is True
            assert chats[0].attributes["openai.agents.operation.estimated_end"] is True
            assert chats[0].attributes["openai.agents.operation.outcome"] == "interrupted"
            coverage = [
                span
                for span in native.get_ordered_spans()
                if span.span_data.type == "custom"
                and span.span_data.name == "codex.tracing.coverage"
            ]
            assert len(coverage) == 1
            assert coverage[0].span_data.data["coverage"] == "partial"
            exported_coverage = [
                span
                for span in exporter.get_finished_spans()
                if span.name == "codex.tracing.coverage"
            ]
            assert len(exported_coverage) == 1
            assert exported_coverage[0].attributes["openai.agents.operation.coverage"] == "partial"
        assert len(server.requests) == 1
    finally:
        server.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if model_provider is not None:
            await model_provider.aclose()
        server.close()


@pytest.mark.parametrize("provider_name", ["default", "codex"])
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "stream"])
async def test_operation_tracing_transport_retry(
    tmp_path: Path, telemetry: Any, provider_name: str, streamed: bool
) -> None:
    tracer, exporter, native, _ = telemetry
    server = _ResponsesFixture(retry_once=True)
    model_provider = None
    try:
        model_provider = _model_provider(provider_name, tmp_path, server)
        with tracer.start_as_current_span("host_job"):
            config = RunConfig(
                model_provider=model_provider, tracing={"operation_detail": "metadata"}
            )
            agent = Agent(name="worker", model=MODEL)
            if streamed:
                result = Runner.run_streamed(agent, PROMPT, run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                result = await asyncio.wait_for(Runner.run(agent, PROMPT, run_config=config), 15)
        assert result.final_output == ANSWER
        assert len(server.requests) == 2
        assert server.requests[0]["input"] == server.requests[1]["input"]
        models = [
            span for span in native.get_ordered_spans() if span.span_data.type == "generation"
        ]
        assert len(models) == 1
        assert models[0].error is None
        assert models[0].span_data.operation.outcome == "completed"
        assert models[0].span_data.usage["input_tokens"] == 10
        assert models[0].span_data.usage["output_tokens"] == 5
        assert _ns(models[0].started_at) <= server.intervals[0][0] + 1_000_000
        assert _ns(models[0].ended_at) >= server.intervals[-1][1] - 1_000_000
        chats = [
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "chat"
        ]
        assert len(chats) == 1
        assert chats[0].attributes["gen_ai.usage.input_tokens"] == 10
        assert chats[0].attributes["gen_ai.usage.output_tokens"] == 5
    finally:
        if model_provider is not None:
            await model_provider.aclose()
        server.close()


async def test_stock_codex_reports_partial_coverage(
    tmp_path: Path, telemetry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stock = os.environ.get("CODEX_TRACING_STOCK_EXECUTABLE")
    if not stock:
        pytest.skip("Set CODEX_TRACING_STOCK_EXECUTABLE to run the stock binary coverage gate.")
    monkeypatch.setenv("CODEX_TRACING_TEST_EXECUTABLE", stock)
    tracer, exporter, native, _ = telemetry
    server = _ResponsesFixture()
    model_provider = None
    calls: list[str] = []
    try:
        model_provider = _codex_provider(tmp_path, server)

        @tool
        async def lookup(key: str) -> str:
            calls.append(key)
            return TOOL_RESULT

        with tracer.start_as_current_span("host_job") as host:
            result = await asyncio.wait_for(
                Runner.run(
                    Agent(name="worker", model=MODEL, tools=[lookup]),
                    PROMPT,
                    run_config=RunConfig(
                        model_provider=model_provider, tracing={"operation_detail": "metadata"}
                    ),
                ),
                30,
            )
        assert result.final_output == ANSWER
        assert calls == [ARGUMENT]
        assert len(server.requests) == 2
        spans = native.get_ordered_spans()
        assert not [span for span in spans if span.span_data.type in {"response", "generation"}]
        assert len([span for span in spans if span.span_data.type == "function"]) == 1
        coverage = [
            span
            for span in spans
            if span.span_data.type == "custom" and span.span_data.name == "codex.tracing.coverage"
        ]
        assert len(coverage) == 2
        assert all(
            span.span_data.data["coverage"] == "partial"
            and span.span_data.data["reason"] == "unsupported"
            for span in coverage
        )
        exported = exporter.get_finished_spans()
        assert not [
            span for span in exported if span.attributes.get("gen_ai.operation.name") == "chat"
        ]
        assert (
            len(
                [
                    span
                    for span in exported
                    if span.attributes.get("gen_ai.operation.name") == "execute_tool"
                ]
            )
            == 1
        )
        assert len([span for span in exported if span.name == "codex.tracing.coverage"]) == 2
        assert all(span.context.trace_id == host.get_span_context().trace_id for span in exported)
    finally:
        if model_provider is not None:
            await model_provider.aclose()
        server.close()
