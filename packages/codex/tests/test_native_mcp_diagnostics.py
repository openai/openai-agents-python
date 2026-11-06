"""Caller-safe native MCP setup diagnostics, including real stdio startup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agents import Agent, NativeError, NativeProvider, StdioMCP
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_mcp_stdio import assert_stopped, records

SERVER = Path(__file__).parent / "fixtures/native_stdio_mcp.py"


@pytest.mark.parametrize(
    ("selected", "args", "diagnosis"),
    [
        (("searhc",), (), "selected tools unavailable: 'searhc'"),
        (("search",), ("--invalid-init",), "connection not ready"),
    ],
)
async def test_native_mcp_setup_errors_identify_caller_selection_without_private_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: tuple[str, ...],
    args: tuple[str, ...],
    diagnosis: str,
) -> None:
    marker = "synthetic-private-mcp-setup-value"
    monkeypatch.setenv("N24_MCP_CURRENT", marker)
    log = tmp_path / "server.jsonl"
    descriptor = StdioMCP(
        name="docs",
        command=sys.executable,
        args=(
            "-I",
            "-B",
            str(SERVER),
            "--log",
            str(log),
            "--label",
            marker,
            "--env-var",
            "N24_MCP_CURRENT",
            *args,
        ),
        env_vars=("N24_MCP_CURRENT",),
        tools=selected,
        startup_timeout=2,
    )
    provider = NativeProvider(base_url="http://127.0.0.1:9/v1", api_key=marker)
    assert marker not in repr(descriptor) + repr(provider)
    async with timeout(12):
        async with Agent(
            instructions="Search.", tools=[descriptor], provider=provider
        ).session() as session:
            assert not records(log)
            selected = await session.submit("Search")
            with pytest.raises(NativeError) as caught:
                await selected.get_result()
            await assert_stopped(log)
        await assert_stopped(log)
    message = str(caught.value)
    assert message == f"Native MCP server 'docs': {diagnosis}"
    assert marker not in message
    assert str(SERVER) not in message
    assert "127.0.0.1" not in message
    assert "N24_MCP_CURRENT" not in message


async def test_native_ready_mcp_checkpoint_excludes_current_launch_details(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "synthetic-private-mcp-launch-value"
    monkeypatch.setenv("N24_MCP_CURRENT", marker)
    log = tmp_path / "server.jsonl"
    descriptor = StdioMCP(
        name="docs",
        command=sys.executable,
        args=("-I", "-B", str(SERVER), "--log", str(log), "--label", marker),
        env_vars=("N24_MCP_CURRENT",),
        tools=("search",),
    )
    provider = NativeProvider(base_url=responses.url, api_key=marker, supports_websockets=False)
    async with timeout(12):
        async with Agent(
            instructions="Search.", tools=[descriptor], provider=provider
        ).session() as session:
            assert not records(log) and not responses.requests
            responses.respond(assistant_output("ready"))
            selected = await session.submit("Search")
            assert (await selected.get_result()).final_output == "ready"
            assert records(log)
            state = await session.save_state()
            encoded = state.to_json()
            assert json.loads(encoded)["managed_tool_specs"] == []
            for private in (marker, str(SERVER), str(log), "N24_MCP_CURRENT", "127.0.0.1"):
                assert private not in encoded
        await assert_stopped(log)
