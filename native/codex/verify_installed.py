"""Verify an installed wheel in a clean virtual environment with Python -I.

The local HTTP peer replaces model inference while the real public Runner and
CodexProvider own requests, events, approvals, tools, and native cleanup.
"""

import argparse
import asyncio
import base64
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path

EXPECTED_REVISION = "6ff1e1ca3b30fc8b914720681fcf5fe62e95f856"


def installed_package(pure):
    distribution = metadata.distribution("openai-agents")
    direct = distribution.read_text("direct_url.json")
    assert not (direct and json.loads(direct).get("dir_info", {}).get("editable")), (
        "Verification requires an installed wheel."
    )
    import agents

    libraries = {Path(sysconfig.get_path(kind)).resolve() for kind in ("purelib", "platlib")}
    records = {
        Path(distribution.locate_file(entry)).resolve() for entry in distribution.files or ()
    }
    assert agents.__version__ == distribution.version, "Public and installed versions differ."
    package = Path(agents.__file__).resolve()
    assert any(package.is_relative_to(base) for base in libraries), package
    assert package in records, "Imported SDK does not belong to the installed distribution."
    assert importlib.util.find_spec("agents.models._codex_app_server") is None, (
        "The wheel still contains the retired app-server adapter."
    )
    native = importlib.util.find_spec("agents.models._codex_native")
    if pure:
        assert native is None, "The pure wheel unexpectedly contains the native extension."
        assert not (package.parent / "models" / "_codex_bin").exists(), (
            "The pure wheel unexpectedly contains native helpers."
        )
        return distribution.version
    assert native is not None and native.origin, "The wheel lacks the native extension."
    extension = Path(native.origin).resolve()
    assert any(
        str(extension).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
    ), "The installed native module is not a compiled extension."
    assert extension in records and any(extension.is_relative_to(base) for base in libraries)
    names = ["codex-sdk-runtime", "codex-code-mode-host"]
    if sys.platform == "linux":
        names.extend(("codex-linux-sandbox", "codex-resources/bwrap"))
    if sys.platform == "win32":
        names.extend(("codex-windows-sandbox-setup", "codex-command-runner"))
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in names:
        helper = package.parent / "models" / "_codex_bin" / (name + suffix)
        assert helper.resolve() in records and helper.is_file(), f"Missing packaged helper {name}."
        assert os.access(helper, os.X_OK), f"Packaged helper is not executable {name}."
    native_module = importlib.import_module("agents.models._codex_native")
    assert native_module.CODEX_REVISION == EXPECTED_REVISION, "Native source revision mismatch."
    return distribution.version


async def default_route():
    # This import guard tests the lazy optional boundary through a real default run.
    class ForbidNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "agents.models._codex_native":
                raise AssertionError("The default SDK path imported native code.")

    guard = ForbidNative()
    sys.meta_path.insert(0, guard)
    try:
        import httpx2
        from openai import AsyncOpenAI

        from agents import Agent, RunConfig, Runner, set_default_openai_client
        from agents.models.codex import CodexProvider  # noqa: F401

        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx2.Response(
                200,
                json={
                    "id": "default-response",
                    "object": "response",
                    "status": "completed",
                    "model": "synthetic",
                    "created_at": 1,
                    "parallel_tool_calls": False,
                    "tool_choice": "auto",
                    "tools": [],
                    "usage": None,
                    "output": [
                        {
                            "id": "default-message",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "default Python route",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key="synthetic-default-key", http_client=http) as client:
                set_default_openai_client(client, use_for_tracing=False)
                result = await Runner.run(
                    Agent(name="Default"), "hello", run_config=RunConfig(tracing_disabled=True)
                )
        assert result.final_output == "default Python route" and calls == ["/v1/responses"]
        assert "agents.models._codex_native" not in sys.modules
    finally:
        sys.meta_path.remove(guard)


async def native_route(folder):
    from unittest.mock import Mock

    from pydantic import BaseModel

    from agents import Agent, RunConfig, Runner, RunState
    from agents.decorators import tool
    from agents.memory.resumable_session import ResumableSession
    from agents.models.codex import CodexProvider
    from agents.tracing import TracingProcessor, set_trace_processors, set_tracing_disabled

    class Answer(BaseModel):
        label: str

    image_path = folder / "workspace" / "trace-image.png"
    requests = []
    approval_item = None
    errors = []
    tool_calls = []
    live_delta = threading.Event()
    dynamic_delta = threading.Event()
    blocked_delta = threading.Event()
    release_blocked = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_event(self, event):
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        def do_POST(self):
            try:
                assert self.path == "/v1/responses", "Unexpected request path."
                assert self.headers.get("Authorization") == "Bearer synthetic-installed-key", (
                    "The request lacks the explicit provider API key."
                )
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                assert len(requests) <= 13, "Unexpected extra inference request."
                number = len(requests)
                history = json.dumps(body["input"])
                response = {"id": f"response-{number}"}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.send_event({"type": "response.created", "response": response})
                if number > 9 and number % 2 == 0:
                    assert approval_item is not None
                    item = approval_item
                elif number == 1:
                    item = {
                        "type": "function_call",
                        "call_id": "route-call",
                        "name": "transfer_to_specialist",
                        "arguments": "{}",
                    }
                elif number == 2:
                    item = {
                        "type": "function_call",
                        "call_id": "lookup-call",
                        "name": "lookup",
                        "arguments": '{"value":"record"}',
                    }
                elif number == 8:
                    item = {
                        "type": "function_call",
                        "call_id": "native-image-call",
                        "name": "view_image",
                        "arguments": json.dumps({"path": str(image_path)}),
                    }
                else:
                    if "approval-input" in history:
                        text = "approval resolved"
                    elif "content-input" in history:
                        text = "native-content"
                    elif "blocked-input" in history:
                        assert (
                            "survivor-input" not in history and "after-cancel-input" not in history
                        )
                        text = "blocked"
                    elif "survivor-input" in history:
                        assert "after-cancel-input" not in history
                        text = "survived"
                    elif "after-cancel-input" in history:
                        text = "reused"
                    elif "fresh-input" in history:
                        text = "fresh thread"
                    else:
                        assert "private-input" in history and "retrieved-record" in history
                        assert "route-call" in history and "lookup-call" in history
                        assert body["text"]["format"]["strict"] is True
                        text = '{"label":"found record"}'
                    item = {
                        "id": f"message-{number}",
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": ""}],
                    }
                    self.send_event({"type": "response.output_item.added", "item": item})
                    self.send_event({"type": "response.output_text.delta", "delta": text})
                    if text == "fresh thread":
                        assert live_delta.wait(10), "Streaming buffered until completion."
                    if text == "blocked":
                        assert blocked_delta.wait(10), "The blocked run did not stream."
                        assert release_blocked.wait(10), "Cancellation cleanup did not finish."
                        # The caller cancelled this stream after its first delta.
                        return
                    item["content"][0]["text"] = text
                self.send_event({"type": "response.output_item.done", "item": item})
                if number == 1:
                    assert dynamic_delta.wait(10), "The handoff did not stream."
                    self.send_event(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "id": "late-reasoning",
                                "type": "reasoning",
                                "summary": [
                                    {"type": "summary_text", "text": "Late public summary."}
                                ],
                                "encrypted_content": "opaque-late-reasoning",
                            },
                        }
                    )
                response["usage"] = {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "input_tokens_details": {"cached_tokens": 2},
                    "output_tokens_details": {"reasoning_tokens": 1},
                }
                self.send_event({"type": "response.completed", "response": response})
            except Exception as error:
                errors.append(f"{type(error).__name__} {error}")

    @tool(needs_approval=True)
    def lookup(value: str) -> str:
        """Look up one record."""
        tool_calls.append(value)
        return "retrieved-record"

    specialist = Agent(name="Specialist", tools=[lookup], output_type=Answer)
    agent = Agent(name="Router", handoffs=[specialist])
    plain = Agent(name="Plain")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = folder / "codex" / "config.toml"
    config_path.write_text(
        f"""
model = "gpt-5.4"
model_provider = "installed"
approval_policy = "never"
sandbox_mode = "read-only"
check_for_update_on_startup = false
[features]
plugins = false
code_mode = false
hooks = false
[model_providers.installed]
name = "Installed wheel synthetic peer"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 0
request_max_retries = 0
""",
        encoding="utf-8",
    )
    provider = CodexProvider(cwd=folder / "workspace", api_key="synthetic-installed-key")
    # The isolated worker collects spans locally, without a network exporter.
    processor = Mock(spec=TracingProcessor)
    set_trace_processors([processor])
    set_tracing_disabled(False)
    config = RunConfig(
        model_provider=provider,
        trace_include_sensitive_data=False,
        tracing={"operation_detail": "metadata"},
    )
    try:
        paused = Runner.run_streamed(agent, "private-input", run_config=config)
        async for event in paused.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.function_call_arguments.done"
                and event.data.item_id == "route-call"
            ):
                dynamic_delta.set()
        assert len(paused.interruptions) == 1 and len(requests) == 2 and tool_calls == []
        assert any(
            item.type == "reasoning" and item.encrypted_content == "opaque-late-reasoning"
            for item in paused.raw_responses[0].output
        ), "The handoff dropped reasoning before the peer's terminal event."
        snapshot = json.loads(json.dumps(paused.to_state().to_json()))
        state = await RunState.from_json(agent, snapshot)
        state.approve(state.get_interruptions()[0])
        result = await Runner.run(agent, state, run_config=config)
        assert result.final_output == Answer(label="found record")
        assert result.last_agent is specialist and tool_calls == ["record"]
        usage = result.context_wrapper.usage
        assert (usage.requests, usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
            3,
            36,
            9,
            45,
        )
        assert usage.input_tokens_details.cached_tokens == 6
        assert usage.output_tokens_details.reasoning_tokens == 3
        spans = [call.args[0].span_data for call in processor.on_span_end.call_args_list]
        samples = [span for span in spans if span.type == "generation"]
        assert len(samples) == 3
        assert all(span.input is None and span.output is None for span in samples)
        assert all(span.usage["input_tokens"] == 12 for span in samples)

        config.tracing_disabled = True
        streamed = Runner.run_streamed(plain, "fresh-input", run_config=config)
        deltas = []
        async for event in streamed.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                deltas.append(event.data.delta)
                live_delta.set()
        assert streamed.final_output == "fresh thread" and "".join(deltas) == "fresh thread"
        history = json.dumps(requests[3]["input"])
        assert "private-input" not in history and "retrieved-record" not in history
        assert streamed.context_wrapper.usage.total_tokens == 15

        blocked = Runner.run_streamed(plain, "blocked-input", run_config=config)

        async def consume_blocked():
            async for event in blocked.stream_events():
                if (
                    event.type == "raw_response_event"
                    and event.data.type == "response.output_text.delta"
                ):
                    blocked_delta.set()

        consumer = asyncio.create_task(consume_blocked())
        try:
            assert await asyncio.to_thread(blocked_delta.wait, 10), "The blocked run did not start."
            survivor = await Runner.run(plain, "survivor-input", run_config=config)
            assert survivor.final_output == "survived"
            blocked.cancel()
            await consumer
        finally:
            release_blocked.set()
            if not consumer.done():
                blocked.cancel()
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        reused = await Runner.run(plain, "after-cancel-input", run_config=config)
        assert reused.final_output == "reused" and len(requests) == 7
        image_path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNoaGgAAAMEAYFL09IQAAAAAElFTkSuQmCC"
            )
        )
        before = len(processor.on_span_end.call_args_list)
        content_config = RunConfig(
            model_provider=provider,
            trace_include_sensitive_data=True,
            tracing={"operation_detail": "content"},
        )
        content_result = await Runner.run(plain, "content-input", run_config=content_config)
        assert content_result.final_output == "native-content" and len(requests) == 9
        captured = [
            call.args[0].span_data for call in processor.on_span_end.call_args_list[before:]
        ]
        native_models = [span for span in captured if span.type == "generation"]
        native_tools = [
            span
            for span in captured
            if span.type == "function" and span.name in {"view_image", "functions.view_image"}
        ]
        request_sizes = [len(json.dumps(body).encode()) for body in requests[-2:]]
        assert len(native_models) == 2 and len(native_tools) == 1
        assert "content-input" in json.dumps(native_models[0].input), (
            f"Native content omitted; request sizes: {request_sizes}"
        )
        assert "native-content" in json.dumps(native_models[-1].output)
        assert str(image_path) in native_tools[0].input
        assert "omitted" in native_tools[0].output
        rendered = json.dumps([span.export() for span in captured])
        assert (
            "synthetic-installed-key" not in rendered
            and "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB" not in rendered
        )
        assert "opaque-late-reasoning" not in rendered

        class ApprovalStore:
            value = None

            async def load(self, _session_id):
                return self.value

            async def save(self, _session_id, value, *, expected):
                if self.value != expected:
                    return False
                self.value = value
                return True

        def fresh_provider():
            return CodexProvider(
                cwd=folder / "workspace",
                api_key="synthetic-installed-key",
                config_overrides=[
                    'approval_policy="on-request"',
                    'approvals_reviewer="user"',
                ],
            )

        await provider.aclose()
        approval_checks = []
        for approved in (False, True):
            target = folder / "workspace" / f"builtin-command-{approved}.txt"
            call_id = f"builtin-command-{approved}"
            approval_item = {
                "type": "function_call",
                "name": "exec_command",
                "call_id": call_id,
                "arguments": json.dumps(
                    {
                        "cmd": f'echo builtin-approved > "{target}"',
                        "login": False,
                        "sandbox_permissions": "require_escalated",
                    }
                ),
            }
            provider = fresh_provider()
            options = RunConfig(model_provider=provider, tracing_disabled=True)
            store = ApprovalStore()
            session = await ResumableSession.create(store)
            paused = await Runner.run(plain, "approval-input", session=session, run_config=options)
            assert len(paused.interruptions) == 1 and not target.exists()
            stored = store.value
            await provider.aclose()
            provider = fresh_provider()
            options.model_provider = provider
            recovered = await ResumableSession.resume(store, session.session_id)
            state = await recovered.load_state(plain)
            assert store.value == stored and not target.exists()
            (pending,) = state.get_interruptions()
            assert pending.raw_item.call_id == call_id
            state.approve(pending) if approved else state.reject(pending)
            result = await Runner.run(plain, state, run_config=options)
            assert result.final_output == "approval resolved"
            assert target.exists() == approved
            if approved:
                assert b"builtin-approved" in target.read_bytes().replace(b"\x00", b"")
            approval_checks.append("approve" if approved else "deny")
            await provider.aclose()
        assert "agents.models._codex_native" in sys.modules
        assert "agents.models._codex_app_server" not in sys.modules
        assert not config_path.with_name("auth.json").exists(), "The API key persisted to disk."
        assert not errors, errors
        return {
            "provider_requests": len(requests),
            "native_content_request_bytes": request_sizes,
            "native_content_roundtrip": True,
            "tool_effects": len(tool_calls),
            "resumed_tokens": usage.total_tokens,
            "live_streaming": True,
            "concurrent_survivor_and_reuse_after_cancel": True,
            "native_approval_checks": approval_checks,
        }
    finally:
        dynamic_delta.set()
        release_blocked.set()
        try:
            await provider.aclose()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive(), "The synthetic server did not stop."


async def verify(pure, folder):
    assert sys.prefix != sys.base_prefix, "Verification requires a virtual environment."
    assert sys.flags.isolated and not sys.flags.optimize, "Run Python with -I and without -O."
    await default_route()
    report = {"passed": True, "version": installed_package(pure)}
    if not pure:
        report["native"] = await native_route(folder)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pure", action="store_true", help="verify the ordinary pure Python wheel")
    parser.add_argument("--output", type=Path, help="write the JSON receipt here")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        try:
            report = asyncio.run(asyncio.wait_for(verify(args.pure, args.worker), timeout=90))
        except Exception as error:
            report = {
                "passed": False,
                "error": f"{type(error).__name__} {error}",
                "traceback": traceback.format_exc(),
            }
        rendered = json.dumps(report, indent=2) + "\n"
        print(rendered, end="")
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        return 0 if report["passed"] else 1
    with tempfile.TemporaryDirectory(prefix="agents-wheel-") as temporary:
        folder = Path(temporary)
        for name in ("codex", "workspace", "tmp"):
            (folder / name).mkdir()
        # Preserve platform runtime settings while isolating user config and credentials.
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LD_LIBRARY_PATH"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(
            {
                "HOME": temporary,
                "USERPROFILE": temporary,
                "CODEX_HOME": str(folder / "codex"),
                "TMPDIR": str(folder / "tmp"),
                "TEMP": str(folder / "tmp"),
                "TMP": str(folder / "tmp"),
                "NO_PROXY": "127.0.0.1,localhost",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "OPENAI_AGENTS_DISABLE_TRACING": "1",
            }
        )
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker", temporary]
        if args.pure:
            command.append("--pure")
        if args.output is not None:
            command.extend(("--output", str(args.output.resolve())))
        return subprocess.run(command, cwd=folder / "workspace", env=env, timeout=110).returncode


if __name__ == "__main__":
    raise SystemExit(main())
