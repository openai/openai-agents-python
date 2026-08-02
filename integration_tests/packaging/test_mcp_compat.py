from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path

import pytest

from agents.mcp import MCPServerStdio

pytestmark = pytest.mark.mcp_compat

LEGACY_SERVER_PATH = Path(__file__).with_name("mcp_legacy_server.py")


@pytest.mark.asyncio
async def test_packaged_client_supports_mcp_v1() -> None:
    expected_version = os.environ["OPENAI_AGENTS_INTEGRATION_MCP_VERSION"]
    assert importlib.metadata.version("mcp") == expected_version

    server = MCPServerStdio(
        name="legacy-test-server",
        params={"command": sys.executable, "args": [str(LEGACY_SERVER_PATH)]},
    )

    async with server:
        tools = await server.list_tools()
        result = await server.call_tool("legacy_tool", {})

    assert [tool.name for tool in tools] == ["legacy_tool"]
    assert getattr(result, "isError", getattr(result, "is_error", None)) is False
    assert result.content[0].type == "text"
    assert result.content[0].text == "legacy-result"
