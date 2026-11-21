"""Exercise the SDK and embedded Codex against a loopback Responses endpoint."""

import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from importlib.util import find_spec
from threading import Thread

import pytest
from openai import AsyncOpenAI

from agents import Agent, NativeTool, RunConfig, RunHooks, Runner, ToolExecutionConfig
from agents.mcp import MCPServerStdio
from agents.mcp._compat import result_is_error
from agents.models._codex_hook_relay import serve_hook
from agents.models.codex import CodexProvider
from agents.models.openai_provider import OpenAIProvider
from agents.tool import ShellCallOutcome, ShellCommandOutput, ShellResult, ShellTool

from ..mcp.helpers import FakeMCPServer


@pytest.fixture
def model_endpoint():
    # A loopback model exposes the tools offered by Codex and the exact inputs to its next turn.
    requests = []
    items = []

    class Responses(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/v1/responses" or not items:
                self.send_error(400)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            step = items.pop(0)
            item = step(request) if callable(step) else step
            response = {
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "model": request.get("model", "gpt-5.4"),
                "status": "completed",
                "output": item if isinstance(item, list) else [item],
                "parallel_tool_calls": True,
                "tools": [],
                "tool_choice": "auto",
            }
            events = [
                {"type": "response.created", "response": response},
                *(
                    {"type": "response.output_item.done", "item": value}
                    for value in response["output"]
                ),
                {"type": "response.completed", "response": response},
            ]
            body = (
                "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                if request.get("stream")
                else json.dumps(response)
            ).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if request.get("stream") else "application/json"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests, items
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def final_message():
    return {
        "type": "message",
        "id": "answer",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }


def codex_fixture(tmp_path, port, monkeypatch, extra=(), **options):
    if find_spec("agents.models._codex_native") is None:
        pytest.skip("Install the SDK's embedded Codex extension to exercise this path.")
    config_home = tmp_path / "codex"
    config_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(config_home))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    return CodexProvider(
        cwd=tmp_path,
        api_key="synthetic-fixture-key",
        config_overrides=[
            'model_provider="fixture"',
            'model_providers.fixture.name="fixture"',
            f'model_providers.fixture.base_url="http://127.0.0.1:{port}/v1"',
            'model_providers.fixture.wire_api="responses"',
            "model_providers.fixture.requires_openai_auth=true",
            "model_providers.fixture.supports_websockets=false",
            "model_providers.fixture.request_max_retries=0",
            "model_providers.fixture.stream_max_retries=0",
            *extra,
        ],
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("harness", ["responses", "codex"])
async def test_shell_and_mcp_hooks_over_responses(tmp_path, harness, model_endpoint, monkeypatch):
    port, requests, items = model_endpoint
    action = {"commands": ["proposed"]}
    first = (
        {
            "type": "function_call",
            "name": "shell",
            "namespace": "agents_sdk",
            "call_id": "cmd",
            "arguments": json.dumps({"action": action}),
        }
        if harness == "codex"
        else {
            "type": "shell_call",
            "id": "sh_1",
            "call_id": "cmd",
            "action": action,
            "status": "in_progress",
        }
    )
    items.extend(
        [
            first,
            {
                "type": "function_call",
                "name": "lookup_record",
                "call_id": "lookup",
                "arguments": json.dumps({"query": "original"}),
            },
            final_message(),
        ]
    )
    client = AsyncOpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="synthetic-fixture-key")
    provider = OpenAIProvider(openai_client=client, use_responses=True)
    try:
        if harness == "codex":
            provider = codex_fixture(tmp_path, port, monkeypatch)
        effects = []

        def shell(request):
            effects.extend(request.data.action.commands)
            return ShellResult(
                [
                    ShellCommandOutput(
                        stderr="private diagnostic",
                        outcome=ShellCallOutcome(type="exit", exit_code=7),
                    )
                ]
            )

        mcp = FakeMCPServer()
        mcp.add_tool(
            "lookup_record",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

        async def before(call):
            if call.tool.name == "shell":
                return {"action": {"commands": ["accepted"]}}
            assert call.tool.name == "lookup_record"
            return {"query": "safe"}

        async def after(call, result):
            if call.tool.name == "shell":
                assert result.output[0].exit_code == 7
                result.output[0].stderr = "safe diagnostic"
                return result
            assert call.arguments == {"query": "safe"}
            return "Record found"

        result = await asyncio.wait_for(
            Runner.run(
                Agent(
                    name="test",
                    model="gpt-5.4",
                    tools=[ShellTool(executor=shell)],
                    mcp_servers=[mcp],
                ),
                "Use both tools.",
                run_config=RunConfig(
                    model_provider=provider,
                    tracing_disabled=True,
                    tool_execution=ToolExecutionConfig(
                        prepare_tool_input=before, prepare_tool_output=after
                    ),
                ),
            ),
            timeout=40,
        )
        assert result.final_output == "done"
        if harness == "codex":
            native = {
                member.get("name")
                for spec in requests[0]["tools"]
                if spec.get("name") != "agents_sdk"
                for member in spec.get("tools", [spec])
            }
            assert native.isdisjoint(
                {"exec_command", "write_stdin", "apply_patch", "view_image", "shell_command"}
            )
        assert effects == ["accepted"]
        assert mcp.tool_results == ['result_lookup_record_{"query": "safe"}']
        cmd = next(
            item
            for item in requests[1]["input"]
            if item.get("call_id") == "cmd"
            and item["type"] in ("function_call_output", "shell_call_output")
        )
        assert "safe diagnostic" in str(cmd) and "private diagnostic" not in str(cmd)
        lookup = next(
            item
            for item in requests[2]["input"]
            if item.get("call_id") == "lookup" and item["type"] == "function_call_output"
        )
        assert "Record found" in str(lookup) and "result_lookup_record" not in str(lookup)
    finally:
        await provider.aclose()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("shell", marks=pytest.mark.requires_native_macos_sandbox),
        pytest.param("patch", marks=pytest.mark.requires_native_macos_sandbox),
        "mcp",
        "blocked-patch",
    ],
)
async def test_native_tool_hooks(tmp_path, model_endpoint, operation, monkeypatch):
    port, requests, items = model_endpoint
    events = []
    patch = "*** Begin Patch\n*** Add File: original.txt\n+created\n*** End Patch"
    extra = [
        "features.code_mode=false",
        'approval_policy="never"',
        'sandbox_mode="workspace-write"',
    ]
    if operation == "shell":
        (tmp_path / "original.txt").write_text("original contents")
        (tmp_path / "prepared.txt").write_text("prepared contents")
    if operation == "mcp":
        script = (
            "try:\n from mcp.server.mcpserver import MCPServer\n"
            "except ImportError:\n from mcp.server.fastmcp import FastMCP as MCPServer\n"
            "server = MCPServer('fixture')\n"
            "@server.tool()\ndef echo(value: str) -> str:\n return value\n"
            "server.run(transport='stdio')\n"
        )
        extra.extend(
            [
                f"mcp_servers.fixture.command={json.dumps(sys.executable)}",
                f"mcp_servers.fixture.args={json.dumps(['-I', '-c', script])}",
                'mcp_servers.fixture.default_tools_approval_mode="approve"',
                "mcp_servers.fixture.required=true",
            ]
        )

    def proposed(request):
        tools = [
            (
                member.get("name") or "",
                spec.get("name") if spec.get("type") == "namespace" else None,
            )
            for spec in request["tools"]
            for member in spec.get("tools", [spec])
        ]
        assert not any("agents_hooks_" in (namespace or name) for name, namespace in tools)
        if operation in ("patch", "blocked-patch"):
            name, namespace = next((n, ns) for n, ns in tools if n == "apply_patch")
            call = {"type": "custom_tool_call", "input": patch}
        elif operation == "shell":
            name, namespace = next((n, ns) for n, ns in tools if n == "exec_command")
            call = {
                "type": "function_call",
                "arguments": json.dumps({"cmd": "cat original.txt", "login": False}),
            }
        else:
            name, namespace = next((n, ns) for n, ns in tools if n.endswith("echo"))
            call = {"type": "function_call", "arguments": json.dumps({"value": "original"})}
        return {
            **call,
            "name": name,
            "call_id": "tool_1",
            **({"namespace": namespace} if namespace else {}),
        }

    items.extend([proposed, final_message()])

    async def before(call):
        assert isinstance(call.tool, NativeTool)
        assert call.call_id == "tool_1"
        events.append(("input", call.tool.name))
        if operation == "blocked-patch":
            raise ValueError("fixture declined")
        if operation == "mcp":
            return {"value": "prepared"}
        return {"command": call.arguments["command"].replace("original.txt", "prepared.txt")}

    async def after(call, result):
        events.append(("output", result))
        if operation == "mcp":
            assert call.arguments == {"value": "prepared"}
        return "published result"

    class Observer(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            events.append(("start", tool.name))

        async def on_tool_end(self, context, agent, tool, result):
            events.append(("end", result))

    provider = codex_fixture(tmp_path, port, monkeypatch, extra)
    if operation == "mcp":
        # A normal CIDR exclusion still routes model HTTP directly. Native hooks use stdio.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "127.0.0.0/8")
        monkeypatch.setenv("no_proxy", "127.0.0.0/8")
    config = RunConfig(
        model_provider=provider,
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(prepare_tool_input=before, prepare_tool_output=after),
    )
    # A model without native tool search receives MCP definitions directly.
    agent = Agent(name="test", model="fixture-direct-tools" if operation == "mcp" else "gpt-5.5")
    try:
        if operation == "blocked-patch":
            with pytest.raises(ValueError, match="fixture declined"):
                await Runner.run(agent, "Run the tool.", hooks=Observer(), run_config=config)
            assert [stage for stage, _ in events] == ["input"]
            assert (
                not (tmp_path / "original.txt").exists()
                and not (tmp_path / "prepared.txt").exists()
            )
            return
        if operation == "shell":
            streamed = Runner.run_streamed(
                agent, "Run the tool.", hooks=Observer(), run_config=config
            )
            async for _ in streamed.stream_events():
                pass
            assert streamed.final_output == "done"
        else:
            result = await Runner.run(agent, "Run the tool.", hooks=Observer(), run_config=config)
            assert result.final_output == "done"
        assert [stage for stage, _ in events] == ["input", "start", "output", "end"]
        assert events[-1][1] == "published result"
        outputs = [
            item["output"]
            for item in requests[1]["input"]
            if item.get("call_id") == "tool_1"
            and item["type"] in ("custom_tool_call_output", "function_call_output")
        ]
        assert outputs == ["published result"]
        if operation == "patch":
            assert (tmp_path / "prepared.txt").read_text().strip() == "created"
            assert not (tmp_path / "original.txt").exists()
        else:
            assert "prepared" in str(events[2][1])
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_native_hooks_respect_caller_disablement(tmp_path, model_endpoint, monkeypatch):
    port, requests, _ = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, ["features.hooks=false"])

    async def before(call):
        pytest.fail("The caller disabled native hooks.")

    try:
        run_config = RunConfig(
            model_provider=provider,
            tracing_disabled=True,
            tool_execution=ToolExecutionConfig(prepare_tool_input=before),
        )
        with pytest.raises(RuntimeError, match="native tool hooks are disabled"):
            await Runner.run(
                Agent(name="test", model="gpt-5.4"), "Run a tool.", run_config=run_config
            )
        assert requests == []
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_native_hook_endpoint_rejects_untrusted_calls():
    executable = (
        files("agents.models")
        / "_codex_bin"
        / ("codex-sdk-runtime.exe" if os.name == "nt" else "codex-sdk-runtime")
    )
    if not executable.is_file():
        pytest.skip("Install the SDK's embedded Codex runtime helper to exercise this path.")
    received = []

    async def dispatch(arguments):
        received.append(arguments)
        return {}

    key = "fixture-capability"
    async with serve_hook(key, dispatch) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"x" * len(key))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
        async with MCPServerStdio(
            {
                "command": str(executable),
                "args": ["--sdk-hook-stdio", str(port)],
                "env": {"AGENTS_CODEX_HOOK_KEY": key},
            }
        ) as client:
            assert await client.list_tools() == []
            # An invocation needs its own credential even on an authenticated connection.
            assert result_is_error(await client.call_tool("dispatch", {"event": "PreToolUse"}))
            assert received == []
            accepted = await client.call_tool("dispatch", {"event": "PreToolUse", "key": key})
            assert not result_is_error(accepted)
        assert received == [{"event": "PreToolUse"}]
