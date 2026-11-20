"""Exercise the SDK and embedded Codex against a loopback Responses endpoint."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from threading import Thread

import pytest
from openai import AsyncOpenAI

from agents import Agent, RunConfig, Runner, ToolExecutionConfig
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
                "output": [item],
                "parallel_tool_calls": True,
                "tools": [],
                "tool_choice": "auto",
            }
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.done", "item": item},
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
