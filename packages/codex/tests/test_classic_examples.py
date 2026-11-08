"""The restored Classic runner discovers runnable modules from the SDK root."""

import ast
import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from examples.classic.run_examples import discover_examples


def test_classic_runner_discovers_modules_from_repository_root():
    examples = discover_examples([])
    modules = {example.module for example in examples}
    assert "examples.classic.basic.hello_world" in modules
    assert "examples.classic.run_examples" not in modules
    assert all(module.startswith("examples.classic.") for module in modules)
    root = Path(__file__).resolve().parents[3]
    assert all(root / example.relpath == example.path for example in examples)


@pytest.mark.skipif(os.name != "posix", reason="Capabilities example requires a Unix sandbox")
def test_classic_capabilities_manifest_contains_readable_image(tmp_path):
    from agents.classic.sandbox import LocalFile
    from examples.classic.sandbox.sandbox_agent_capabilities import _build_manifest

    manifest = _build_manifest(tmp_path)
    image = manifest.entries["examples/classic/image.png"]
    assert isinstance(image, LocalFile)
    assert image.src.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def runner_dry_run(tmp_path, relative_path, *, include_external=False):
    root = Path(__file__).resolve().parents[3]
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EXAMPLES_")
    }
    environment["SHELL"] = ""
    environment["PYTHONPATH"] = os.pathsep.join((str(root), environment.get("PYTHONPATH", "")))
    command = [
        sys.executable,
        "-m",
        "examples.classic.run_examples",
        "--filter",
        relative_path,
        "--dry-run",
        "--include-interactive",
        "--include-server",
        "--include-audio",
        "--logs-dir",
        str(tmp_path / "logs"),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
        "--main-log",
        str(tmp_path / "main.log"),
    ]
    if include_external:
        command.append("--include-external")
    return subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.parametrize(
    "filter_text", ["sandbox/basic.py", "sandbox\\basic.py", "SANDBOX\\BASIC.PY"]
)
def test_runner_filters_accept_both_separators_and_ignore_case(tmp_path, filter_text):
    output = runner_dry_run(tmp_path, filter_text, include_external=True)
    assert "- RUN  examples/classic/sandbox/basic.py" in output


@pytest.mark.parametrize("filter_text", ["tools/", "tools\\"])
def test_runner_filters_preserve_trailing_directory_separator(filter_text):
    modules = {example.module for example in discover_examples([filter_text])}
    assert "examples.classic.tools.web_search" in modules
    assert "examples.classic.agent_patterns.agents_as_tools" not in modules


@pytest.mark.parametrize(
    "relative_path",
    [
        "sandbox/basic.py",
        "sandbox/tax_prep.py",
        "sandbox/memory_s3.py",
        "sandbox/sandbox_agent_with_remote_snapshot.py",
        "sandbox/docker/docker_runner.py",
        "sandbox/extensions/daytona/daytona_runner.py",
        "sandbox/external_provider/src/example_docker_provider/demo.py",
        "model_providers/any_llm_auto.py",
        "model_providers/any_llm_provider.py",
        "model_providers/custom_example_agent.py",
        "model_providers/custom_example_global.py",
        "model_providers/custom_example_provider.py",
        "model_providers/litellm_auto.py",
        "model_providers/litellm_provider.py",
    ],
)
def test_provider_examples_require_explicit_external_opt_in(tmp_path, relative_path):
    default = runner_dry_run(tmp_path, relative_path)
    assert "Interactive mode: prompt" in default
    assert f"- SKIP examples/classic/{relative_path}" in default
    assert "external" in default
    opted_in = runner_dry_run(tmp_path, relative_path, include_external=True)
    assert f"- RUN  examples/classic/{relative_path}" in opted_in


@pytest.mark.parametrize(
    "relative_path",
    [
        "sandbox/unix_local_runner.py",
        "sandbox/healthcare_support/main.py",
        "sandbox/handoffs.py",
        "sandbox/memory.py",
        "sandbox/sandbox_agent_capabilities.py",
        "sandbox/tutorials/dataroom_qa/main.py",
    ],
)
def test_local_sandbox_examples_keep_default_runner_availability(tmp_path, relative_path):
    output = runner_dry_run(tmp_path, relative_path)
    assert f"- RUN  examples/classic/{relative_path}" in output


def custom_client_readme_namespace():
    readme = (
        Path(__file__).resolve().parents[3]
        / "examples/classic/mcp/streamablehttp_custom_client_example/README.md"
    )
    snippet = readme.read_text(encoding="utf-8").split("```python\n", 1)[1].split("```", 1)[0]
    definitions = ast.parse(snippet, filename=str(readme))
    definitions.body = [
        node
        for node in definitions.body
        if isinstance(node, ast.Import | ast.ImportFrom | ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    namespace = {}
    exec(compile(definitions, str(readme), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("stall_response", [False, True])
@pytest.mark.parametrize("factory_source", ["module", "readme"])
async def test_custom_mcp_http_client_applies_auth_headers_and_read_timeout(
    stall_response, factory_source
):
    import httpx2

    from examples.classic.mcp.streamablehttp_custom_client_example.main import (
        create_custom_http_client,
    )

    if factory_source == "readme":
        create_custom_http_client = custom_client_readme_namespace()["create_custom_http_client"]

    request_seen = asyncio.get_running_loop().create_future()
    release_response = asyncio.Event()
    connections = set()

    async def handle_request(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            request_seen.set_result(request.decode("ascii"))
            if stall_response:
                await release_response.wait()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(task)

    server = await asyncio.start_server(handle_request, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with create_custom_http_client(
            headers={"X-Custom-Client": "configured-example"},
            timeout=httpx2.Timeout(5.0, read=0.1),
            auth=httpx2.BasicAuth("example-user", "example-password"),
        ) as client:
            if stall_response:
                with pytest.raises(httpx2.ReadTimeout):
                    await asyncio.wait_for(client.get(f"http://127.0.0.1:{port}/"), timeout=5)
            else:
                response = await asyncio.wait_for(
                    client.get(f"http://127.0.0.1:{port}/"), timeout=5
                )
                assert response.status_code == 200
                assert response.text == "OK"
            request = await asyncio.wait_for(request_seen, timeout=5)
            credentials = base64.b64encode(b"example-user:example-password").decode("ascii")
            assert f"Authorization: Basic {credentials}\r\n" in request
            assert "X-Custom-Client: configured-example\r\n" in request
    finally:
        release_response.set()
        if connections:
            await asyncio.gather(*connections)
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("body_source", ["module", "readme"])
async def test_custom_mcp_examples_configure_real_wrapper_requests(monkeypatch, body_source):
    from contextlib import nullcontext

    from agents.classic.mcp import MCPServerStreamableHttp
    from examples.classic.mcp.streamablehttp_custom_client_example import main as example
    from tests.test_native_mcp_tools import Peer

    namespace = example.__dict__ if body_source == "module" else custom_client_readme_namespace()
    original_factory = namespace["create_custom_http_client"]
    request_timeouts = []

    def observe_factory(headers=None, timeout=None, auth=None):
        client = original_factory(headers=headers, timeout=timeout, auth=auth)

        async def observe_request(request):
            request_timeouts.append(dict(request.extensions["timeout"]))

        client.event_hooks.setdefault("request", []).append(observe_request)
        return client

    async def list_tools(server):
        assert {tool.name for tool in await server.list_tools()} == {"search", "excluded"}

    peer = Peer()
    async with peer.serve() as url:
        # Redirect only the endpoint; retain every example-supplied transport parameter.
        def local_server(*args, params, **kwargs):
            return MCPServerStreamableHttp(*args, params={**params, "url": url}, **kwargs)

        monkeypatch.setitem(namespace, "MCPServerStreamableHttp", local_server)
        monkeypatch.setitem(namespace, "create_custom_http_client", observe_factory)
        if body_source == "module":
            # Client configuration does not require a model or an exported trace.
            monkeypatch.setitem(namespace, "run_with_custom_client", list_tools)
            monkeypatch.setitem(namespace, "trace", lambda **_kwargs: nullcontext())
        await asyncio.wait_for(namespace["main"](), timeout=10)

    assert any(request.get("method") == "tools/list" for request in peer.requests)
    credentials = base64.b64encode(b"demo-user:demo-password").decode("ascii")
    assert peer.auth and all(header == f"Basic {credentials}" for header in peer.auth)
    assert request_timeouts and all(
        value == {"connect": 60.0, "read": 120.0, "write": 60.0, "pool": 60.0}
        for value in request_timeouts
    )
