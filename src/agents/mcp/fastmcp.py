from pathlib import Path
from typing import Any

from fastmcp.client import Client, ClientTransport
from fastmcp.server import FastMCP
from pydantic import AnyUrl
from mcp.types import CallToolResult, Tool

from ..mcp import MCPServer


class FastMCPServer(MCPServer):
    """
    Support fastmcp transport implementations, include in-memory fastmcp servers.
    """

    def __init__(
        self,
        transport: ClientTransport | FastMCP | AnyUrl | Path | str,
        name: str | None = None,
    ):
        self._client = Client(transport)
        if not name:
            if isinstance(transport, FastMCP):
                name = transport.name
            else:
                name = str(transport)
        self._name = name

    async def connect(self):
        await self._client.__aenter__()

    async def cleanup(self):
        await self._client.__aexit__(None, None, None)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._client.__aexit__(exc_type, exc_value, traceback)

    @property
    def name(self) -> str:
        return self._name

    async def list_tools(self) -> list[Tool]:
        return await self._client.list_tools()

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None
    ) -> CallToolResult:
        return await self._client.call_tool(
            tool_name, arguments, _return_raw_result=True
        )
