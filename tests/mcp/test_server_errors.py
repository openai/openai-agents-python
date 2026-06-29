import builtins
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents import Agent
from agents.exceptions import UserError
from agents.mcp.server import MCPServerStreamableHttp, _MCPServerWithClientSession
from agents.run_context import RunContextWrapper

# Handle Python version compatibility for ExceptionGroups
if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup
else:
    BaseExceptionGroup = builtins.BaseExceptionGroup


class CrashingClientSessionServer(_MCPServerWithClientSession):
    def __init__(self):
        super().__init__(cache_tools_list=False, client_session_timeout_seconds=5)
        self.cleanup_called = False

    def create_streams(self):
        raise ValueError("Crash!")

    async def cleanup(self):
        self.cleanup_called = True
        await super().cleanup()

    @property
    def name(self) -> str:
        return "crashing_client_session_server"


@pytest.mark.asyncio
async def test_server_errors_cause_error_and_cleanup_called():
    server = CrashingClientSessionServer()

    with pytest.raises(ValueError):
        await server.connect()

    assert server.cleanup_called


@pytest.mark.asyncio
async def test_not_calling_connect_causes_error():
    server = CrashingClientSessionServer()

    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    with pytest.raises(UserError):
        await server.list_tools(run_context, agent)

    with pytest.raises(UserError):
        await server.call_tool("foo", {})


@pytest.mark.asyncio
async def test_call_tool_nested_exception_group_mapping():
    """
    Regression test ensuring that nested ExceptionGroups containing HTTP errors
    are recursively extracted and mapped to a UserError in call_tool().
    """
    # 1. Initialize the server with mock streamable parameters
    server = MCPServerStreamableHttp(params={"url": "http://fake-mcp-server"})

    # 2. Simulate an active connection by mocking the session object
    server.session = MagicMock()

    # 3. Construct a nested ExceptionGroup hierarchy containing a connection error
    http_error = httpx.ConnectError("Network unreachable")
    inner_group = BaseExceptionGroup("inner_failures", [http_error])
    outer_group = BaseExceptionGroup("outer_failures", [inner_group])

    # 4 & 5. Mock the internal retry handler to raise the nested group, and assert UserError
    with patch.object(server, "_call_tool_with_isolated_retry", side_effect=outer_group):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool(tool_name="test_tool", arguments={})

    # 6. Verify that the user-facing message is mapped correctly based on the root cause
    assert "Connection lost" in str(exc_info.value)
    assert exc_info.value.__cause__ is http_error
