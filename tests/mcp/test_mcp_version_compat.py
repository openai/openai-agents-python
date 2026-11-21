from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents import UserError
from agents.mcp import MCPServerStdio
from agents.mcp._compat import MCP_V2, MCPError, result_is_error, result_next_cursor

LEGACY_SERVER_PATH = Path(__file__).parent / "servers" / "legacy.py"


@pytest.mark.asyncio
async def test_stdio_connects_to_legacy_server():
    server = MCPServerStdio(
        name="legacy-test-server",
        params={"command": sys.executable, "args": [str(LEGACY_SERVER_PATH)]},
    )

    async with server:
        tools = await server.list_tools()
        result = await server.call_tool("legacy_tool", {})
        protocol_version = getattr(server.session, "protocol_version", None)
        # This tools-only peer rejects every resource request.
        resources = await server.list_resources()
        templates = await server.list_resource_templates()
        assert resources.resources == []
        assert templates.model_dump(by_alias=True)["resourceTemplates"] == []
        assert result_next_cursor(resources) is result_next_cursor(templates) is None
        with pytest.raises(MCPError, match="Unknown method: resources/read"):
            await server.read_resource("demo://reports/latest")

    with pytest.raises(UserError, match="Server not initialized"):
        await server.list_resources()
    assert [tool.name for tool in tools] == ["legacy_tool"]
    assert result.content[0].type == "text"
    assert result_is_error(result) is False
    if MCP_V2:
        assert protocol_version == "2025-06-18"
        assert server.server_initialize_result is not None
