"""Discovered metadata budgets before native tool-name projection."""

from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from agents.classic.mcp import MCPServerStreamableHttp
from agents.migration.mcp import MCPNamespace, select_mcp_tools


@pytest.mark.parametrize("kind", ["name", "utf8", "aggregate", "count"])
async def test_discovered_catalog_limits_preserve_borrowed_connection(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "name":
        names = ["x" * 4097]
    elif kind == "utf8":
        names = ["😀" * 1025]
    elif kind == "aggregate":
        names = [f"{index:04}" + "x" * 4092 for index in range(257)]
    else:
        names = [f"tool_{index}" for index in range(16385)]
    expected = (
        "1048576-byte" if kind == "aggregate" else "16384-tool" if kind == "count" else "4096-byte"
    )
    server = MCPServerStreamableHttp(params={"url": "https://example.invalid/mcp"})
    # A borrowed client's metadata response; no network or model work is needed.
    listing = AsyncMock(
        return_value=[Tool(name=name, inputSchema={"type": "object"}) for name in names]
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(server, "list_tools", listing)
    monkeypatch.setattr(server, "cleanup", cleanup)
    with pytest.raises(ValueError, match=expected):
        await select_mcp_tools(MCPNamespace("records", server), context=None, tool_names=names[:1])
    listing.assert_awaited_once()
    cleanup.assert_not_awaited()


async def test_boundary_sized_discovered_names_keep_native_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["x" * 4096, "😀" * 1024]
    server = MCPServerStreamableHttp(params={"url": "https://example.invalid/mcp"})
    listing = AsyncMock(
        return_value=[Tool(name=name, inputSchema={"type": "object"}) for name in names]
    )
    monkeypatch.setattr(server, "list_tools", listing)
    tools = await select_mcp_tools(MCPNamespace("records", server), context=None, tool_names=names)
    assert len(tools) == len({tool.name for tool in tools}) == 2
    assert all(len(tool.namespace or "") + len(tool.name) + 2 <= 128 for tool in tools)
