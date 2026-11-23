"""Exercise the SDK and embedded Codex against a loopback Responses endpoint."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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


def codex_fixture(tmp_path, port, monkeypatch, extra=()):
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


@pytest.fixture
def hook_executable():
    executable = (
        files("agents.models")
        / "_codex_bin"
        / ("codex-sdk-runtime.exe" if os.name == "nt" else "codex-sdk-runtime")
    )
    if not executable.is_file():
        pytest.skip("Install the SDK's embedded Codex runtime helper to exercise this path.")
    return str(executable)


def test_native_hook_stdio_replies_before_eof(hook_executable):
    with socket.create_server(("127.0.0.1", 0)) as listener, ThreadPoolExecutor(1) as reader:
        listener.settimeout(5)
        with subprocess.Popen(
            [hook_executable, "--sdk-hook-stdio", str(listener.getsockname()[1])],
            env={**os.environ, "AGENTS_CODEX_HOOK_KEY": "x"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as relay:
            try:
                assert relay.stdin is not None and relay.stdout is not None
                with listener.accept()[0] as peer, peer.makefile("rb") as requests:
                    peer.settimeout(5)
                    assert requests.read(1) == b"x"
                    for reply in (b"ok\n", b"\x00\xff"):
                        relay.stdin.write(b"hello\n")
                        relay.stdin.flush()
                        assert requests.readline() == b"hello\n"
                        peer.sendall(reply)
                        # A blocked pipe read must not prevent timeout and child cleanup.
                        assert (
                            reader.submit(relay.stdout.read, len(reply)).result(timeout=5) == reply
                        )
                assert relay.wait(timeout=5) == 0
            finally:
                relay.kill()


@pytest.mark.asyncio
async def test_native_hook_endpoint_rejects_untrusted_calls(hook_executable):
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
                "command": hook_executable,
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


def test_native_mcp_result_registrations():
    async def edit(call, result):
        return result

    pair = (("raw-peer", "lookup"), edit)
    pairs = [pair]
    config = ToolExecutionConfig(native_mcp_result_transforms=pairs)
    pairs.clear()
    assert config.native_mcp_result_transforms == (pair,)
    assert ToolExecutionConfig().native_mcp_result_transforms == ()
    for invalid in (
        {},
        [pair, pair],
        [(("peer", ""), edit)],
        [("lookup", edit)],
        [(pair[0], None)],
    ):
        with pytest.raises(ValueError, match="native"):
            ToolExecutionConfig(native_mcp_result_transforms=invalid)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("first", ["lookup", "_lookup"])
@pytest.mark.parametrize("duplicate", [True, False])
async def test_native_mcp_parallel_editors(tmp_path, model_endpoint, monkeypatch, first, duplicate):
    port, requests, items = model_endpoint
    second = "_lookup" if first == "lookup" else "lookup"
    # Both raw tools have the same PostToolUse name and arguments. With valid IDs,
    # hold the first invocation until the second one reaches its editor.
    script = (
        "import asyncio\nfrom pathlib import Path\n"
        "try:\n from mcp.server.mcpserver import MCPServer\n"
        "except ImportError:\n from mcp.server.fastmcp import FastMCP as MCPServer\n"
        "from mcp.types import ToolAnnotations\n"
        "server = MCPServer('fixture')\n"
        f"root = Path({str(tmp_path)!r})\n"
        "def register(name):\n"
        " @server.tool(name=name, annotations=ToolAnnotations(readOnlyHint=True))\n"
        " async def query(value: str):\n"
        "  (root / name).touch()\n"
        f"  while name == {first!r} and {not duplicate} and not (root / 'ready').exists():\n"
        "   await asyncio.sleep(0.01)\n"
        "  return name\n"
        "register('lookup')\nregister('_lookup')\nserver.run(transport='stdio')\n"
    )
    provider = codex_fixture(
        tmp_path,
        port,
        monkeypatch,
        [
            "features.code_mode=false",
            'approval_policy="never"',
            f"mcp_servers.peer.command={json.dumps(sys.executable)}",
            f"mcp_servers.peer.args={json.dumps(['-I', '-c', script])}",
            'mcp_servers.peer.default_tools_approval_mode="approve"',
            "mcp_servers.peer.required=true",
        ],
    )

    def proposed(request):
        names = {
            tool["name"]: spec["name"]
            for spec in request["tools"]
            if spec.get("type") == "namespace"
            for tool in spec["tools"]
        }
        return [
            {
                "type": "function_call",
                "call_id": "same" if duplicate else name,
                "namespace": names[name],
                "name": name,
                "arguments": '{"value":"same"}',
            }
            for name in (first, second)
        ]

    items.extend([proposed, final_message()])
    seen = []

    async def selected(call, result):
        seen.append("lookup")
        (tmp_path / "ready").touch()
        return {"content": [{"type": "text", "text": "edited"}]}

    async def ordinary(call, result):
        assert "_lookup" in json.dumps(result)
        seen.append("_lookup")
        (tmp_path / "ready").touch()
        return result

    config = RunConfig(
        model_provider=provider,
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(
            native_mcp_result_transforms=[(("peer", "lookup"), selected)],
            prepare_tool_output=ordinary,
        ),
    )
    try:
        run = asyncio.wait_for(
            Runner.run(
                Agent(name="test", model="fixture-direct-tools"), "Look up", run_config=config
            ),
            30,
        )
        if duplicate:
            with pytest.raises(RuntimeError, match="Codex model turn failed"):
                await run
            assert not (tmp_path / second).exists()
            assert seen in ([], [first])
            assert len(requests) == 1
        else:
            assert (await run).final_output == "done"
            assert seen == [second, first]
            assert "edited" in json.dumps(requests[1]["input"])
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_native_mcp_edit_skips_only_one_matching_completion():
    from agents import RunContextWrapper
    from agents.models._codex_tool_hooks import CodexToolHooks
    from agents.models._run_context import ModelToolHooks

    seen = []

    async def legacy(call, result):
        seen.append(result)
        return result

    hooks = CodexToolHooks.for_run(
        ModelToolHooks(
            Agent(name="test"),
            RunContextWrapper(None),
            RunHooks(),
            ToolExecutionConfig(prepare_tool_output=legacy),
        ),
        set(),
    )
    assert hooks is not None
    name = "mcp__peer__lookup"
    request = {"turn_id": "turn", "call_id": "same", "arguments": {"a": True, "b": 2}}
    hooks.confirm_mcp_result(request, name)
    # Two raw names can resolve to the same PostToolUse name.
    for result, changes in (
        ("other peer", {"name": "mcp__other__lookup"}),
        ("other turn", {"turn_id": "other"}),
        ("other input", {"arguments": {"a": 1, "b": 2}}),
        ("selected", {"arguments": {"b": 2, "a": True}}),
        ("later", {}),
    ):
        assert (
            await hooks.decision(
                {"event": "PostToolUse", "name": name, **request, "result": result, **changes}
            )
            == {}
        )
    assert seen == ["other peer", "other turn", "other input", "later"]


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    "mode",
    [
        "replace",
        "reused_id",
        "protected",
        "protected_stream",
        "media",
        "peer_error",
        "force_error",
        "unselected",
        "missing_server",
        "missing_tool",
        "disabled_tool",
        "trace_path",
        "trace_empty",
        "invalid",
        "oversized",
        "surrogate",
        "raise",
        "disclose",
        "cancel",
    ],
)
async def test_native_mcp_result_before_publication(tmp_path, model_endpoint, monkeypatch, mode):
    from importlib import import_module

    from agents import UserError, _debug
    from tests.testing_processor import fetch_ordered_spans

    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", mode != "disclose")
    port, requests, items = model_endpoint
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
        "AAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
    )
    script = (
        "try:\n from mcp.server.mcpserver import MCPServer\n"
        "except ImportError:\n from mcp.server.fastmcp import FastMCP as MCPServer\n"
        "server = MCPServer('fixture')\n"
        "@server.tool()\ndef lookup(value: str):\n"
        " if value == 'fail': raise ValueError('PEER_ONLY')\n"
        " if value == 'media':\n"
        "  from mcp.types import ImageContent, TextContent\n"
        "  return [TextContent(type='text', text='PEER_ONLY'), "
        f"ImageContent(type='image', data='{png}', mimeType='image/png')]\n"
        " return 'PEER_ONLY'\n"
        "@server.tool()\ndef other(): return 'other'\n"
        "server.run(transport='stdio')\n"
    )
    provider = codex_fixture(
        tmp_path,
        port,
        monkeypatch,
        [
            "features.code_mode=false",
            'approval_policy="never"',
            f"mcp_servers.raw-peer.command={json.dumps(sys.executable)}",
            f"mcp_servers.raw-peer.args={json.dumps(['-I', '-c', script])}",
            'mcp_servers.raw-peer.default_tools_approval_mode="approve"',
            "mcp_servers.raw-peer.required=true",
            'mcp_servers.raw-peer.disabled_tools=["other"]'
            if mode == "disabled_tool"
            else "mcp_servers.raw-peer.disabled_tools=[]",
        ],
    )
    changed_input = mode in ("peer_error", "force_error")
    protected = mode in ("protected", "protected_stream")
    argument = {"peer_error": "fail", "media": "media"}.get(mode, "search")

    def proposed(request):
        other = mode == "reused_id" and len(requests) == 2
        spec, name = next(
            (spec, tool["name"])
            for spec in request["tools"]
            for tool in spec.get("tools", [spec])
            if tool.get("name", "").endswith("other" if other else "lookup")
        )
        return {
            "type": "function_call",
            "call_id": "lookup_1",
            "name": name,
            "arguments": "{}"
            if other
            else json.dumps({"value": "initial" if changed_input else argument}),
            **({"namespace": spec["name"]} if spec.get("type") == "namespace" else {}),
        }

    if mode == "reused_id":
        items.append(proposed)
    items.extend([proposed, final_message()])
    events = []
    native_thread = import_module("agents.models._codex_native").Thread
    next_event = native_thread.next_event

    async def record(thread):
        event = await next_event(thread)
        if isinstance(event, str):
            events.append(json.loads(event)["msg"])
        return event

    monkeypatch.setattr(native_thread, "next_event", record)
    entered = asyncio.Event()
    context = object()
    agent = Agent(name="test", model="fixture-direct-tools")

    async def before(call):
        return {"value": argument}

    async def transform(call, result):
        assert call.run_context.context is context and call.agent is agent
        assert (call.tool_namespace, call.tool.name, call.call_id) == (
            "raw-peer",
            "lookup",
            "lookup_1",
        )
        assert call.arguments == {"value": argument}
        assert not entered.is_set()
        entered.set()
        assert bool(result.get("isError")) == (mode == "peer_error")
        if mode in ("raise", "disclose"):
            raise RuntimeError("PEER_ONLY")
        if mode == "surrogate":
            return {"content": [{"type": "text", "text": "REJECTED_ONLY\ud800"}]}
        if mode == "cancel":
            await asyncio.Event().wait()
        if mode == "invalid":
            return {"content": [{"type": "text"}]}
        if mode == "oversized":
            return {"content": [{"type": "text", "text": "x" * (1024 * 1024)}]}
        if mode == "media":
            result["content"][0]["text"] = "published"
            return result
        if protected:
            assert "PEER_ONLY" in json.dumps(result)
            return {
                "content": [{"type": "text", "text": "PEER_ONLY"}],
                "structuredContent": {"summary": "PEER_ONLY"},
                "_meta": {"origin": "fixture", "label": "PEER_ONLY"},
            }
        if mode == "replace":
            return {
                "content": [{"type": "text", "text": "published"}],
                "structuredContent": {"summary": "published"},
                "_meta": {"origin": "fixture"},
            }
        call.arguments.clear()
        result.clear()
        return {
            "content": [{"type": "text", "text": "published"}],
            "isError": mode == "force_error",
        }

    async def legacy(call, result):
        pytest.fail("Neither a selected call nor a rejected duplicate should reach this hook")

    target = {
        "unselected": ("raw-peer", "other"),
        "missing_server": ("raw_peer", "lookup"),
        "missing_tool": ("raw-peer", "old_name"),
        "disabled_tool": ("raw-peer", "other"),
    }.get(mode, ("raw-peer", "lookup"))
    config = RunConfig(
        model_provider=provider,
        redact_text=(lambda text: text.replace("PEER_ONLY", "published")) if protected else None,
        tracing={"operation_detail": "content"},
        trace_include_sensitive_data=True,
        tool_execution=ToolExecutionConfig(
            native_mcp_result_transforms=[(target, transform)],
            prepare_tool_input=before if changed_input else None,
            prepare_tool_output=legacy if mode in ("replace", "reused_id") else None,
        ),
    )

    async def execute():
        if mode in ("media", "surrogate", "protected_stream"):
            streamed = Runner.run_streamed(
                agent, "Look up a value", context=context, run_config=config
            )
            async for event in streamed.stream_events():
                if protected:
                    assert event.type != "raw_response_event"
                assert "PEER_ONLY" not in repr(event)
            return streamed
        return await Runner.run(agent, "Look up a value", context=context, run_config=config)

    try:
        run = execute()
        if mode == "reused_id":
            with pytest.raises(RuntimeError, match="Codex model turn failed"):
                await run
            assert entered.is_set() and len(requests) == 2
            assert not any(
                event["type"] == "raw_response_item"
                and event["item"].get("name", "").endswith("other")
                for event in events
            )
            return
        if mode in ("trace_path", "trace_empty"):
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv(
                "CODEX_ROLLOUT_TRACE_ROOT", str(tmp_path) if mode == "trace_path" else ""
            )
            with pytest.raises(RuntimeError, match="Unset CODEX_ROLLOUT_TRACE_ROOT"):
                await run
            assert not requests
            assert not list(tmp_path.rglob("trace.jsonl"))
            return
        if mode in ("missing_server", "missing_tool", "disabled_tool"):
            with pytest.raises(ValueError, match="available raw tool name"):
                await run
            assert not entered.is_set()
            assert not requests
            return
        if mode == "cancel":
            task = asyncio.create_task(run)
            await asyncio.wait_for(entered.wait(), 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "disclose":
            with pytest.raises(RuntimeError, match="PEER_ONLY"):
                await run
        elif mode in ("raise", "invalid", "oversized", "surrogate"):
            with pytest.raises(UserError, match="Native MCP result transform") as caught:
                await run
            assert caught.value.__cause__ is caught.value.__context__ is None
            assert "PEER_ONLY" not in repr(caught.value)
            assert "REJECTED_ONLY" not in repr(caught.value)
            assert not any(
                frame.frame.f_globals.get("__name__") == "agents.models.codex"
                for frame in caught.traceback
            )
        else:
            result = await run
            assert result.final_output == "done"
            expected = "PEER_ONLY" if mode == "unselected" else "published"
            assert expected in json.dumps(result.to_input_list())
            assert expected in json.dumps(requests[1]["input"])
            completed = next(event for event in events if event["type"] == "mcp_tool_call_end")
            assert expected in json.dumps(completed)
            if protected:
                edited = completed["result"]["Ok"]
                assert edited["content"][0]["text"] == "published"
                assert edited["structuredContent"] == {"summary": "published"}
                assert edited["_meta"] == {"origin": "fixture", "label": "published"}
            if mode == "replace":
                assert completed["result"]["Ok"]["_meta"] == {"origin": "fixture"}
                assert "summary" in json.dumps(requests[1]["input"])
            if mode == "media":
                assert png in json.dumps(completed)
                assert png in json.dumps(requests[1]["input"])
                assert png in json.dumps(result.to_input_list())
            if changed_input:
                assert completed["result"]["Ok"]["isError"]
                call = next(
                    item for item in result.to_input_list() if item.get("type") == "function_call"
                )
                assert json.loads(call["arguments"]) == {"value": argument}
            if mode != "unselected":
                assert "PEER_ONLY" not in json.dumps(result.to_input_list())
        assert entered.is_set() == (mode != "unselected")
    finally:
        await provider.aclose()
    spans = fetch_ordered_spans()
    if mode == "replace":
        (tool,) = [s for s in spans if s.span_data.type == "function"]
        assert "summary" in str(tool.span_data.output)
    if mode != "unselected":
        exported = repr([s.export() for s in spans])
        assert "PEER_ONLY" not in exported
        assert "REJECTED_ONLY" not in exported
        assert "PEER_ONLY" not in json.dumps(events)
        assert "PEER_ONLY" not in json.dumps(requests)
