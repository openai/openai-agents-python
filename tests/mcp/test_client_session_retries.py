from typing import cast

import pytest
from mcp import ClientSession, Tool as MCPTool
from mcp.types import CallToolResult, ListToolsResult

from agents.mcp.server import _MCPServerWithClientSession


class DummySession:
    def __init__(self, fail_call_tool: int = 0, fail_list_tools: int = 0):
        self.fail_call_tool = fail_call_tool
        self.fail_list_tools = fail_list_tools
        self.call_tool_attempts = 0
        self.list_tools_attempts = 0

    async def call_tool(self, tool_name, arguments):
        self.call_tool_attempts += 1
        if self.call_tool_attempts <= self.fail_call_tool:
            raise RuntimeError("call_tool failure")
        return CallToolResult(content=[])

    async def list_tools(self):
        self.list_tools_attempts += 1
        if self.list_tools_attempts <= self.fail_list_tools:
            raise RuntimeError("list_tools failure")
        return ListToolsResult(tools=[MCPTool(name="tool", inputSchema={})])


class DummyServer(_MCPServerWithClientSession):
    def __init__(self, session: DummySession, retries: int):
        super().__init__(
            cache_tools_list=False,
            client_session_timeout_seconds=None,
            max_retry_attempts=retries,
            retry_backoff_seconds_base=0,
        )
        self.session = cast(ClientSession, session)

    def create_streams(self):
        raise NotImplementedError

    @property
    def name(self) -> str:
        return "dummy"


@pytest.mark.asyncio
async def test_call_tool_retries_until_success():
    session = DummySession(fail_call_tool=2)
    server = DummyServer(session=session, retries=2)
    result = await server.call_tool("tool", None)
    assert isinstance(result, CallToolResult)
    assert session.call_tool_attempts == 3


@pytest.mark.asyncio
async def test_list_tools_unlimited_retries():
    session = DummySession(fail_list_tools=3)
    server = DummyServer(session=session, retries=-1)
    tools = await server.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "tool"
    assert session.list_tools_attempts == 4
