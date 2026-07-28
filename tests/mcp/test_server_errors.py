import builtins
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents import Agent
from agents.exceptions import UserError
from agents.mcp.server import MCPServerSse, MCPServerStreamableHttp, _MCPServerWithClientSession
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


_CREDENTIALED_URL = "https://user:s3cr3t_pw@mcp.example.com/sse?api_key=SECRET_QS_KEY"


def _assert_url_credentials_redacted(message: str) -> None:
    assert "s3cr3t_pw" not in message
    assert "SECRET_QS_KEY" not in message
    # The host and path stay so the error still says which server failed.
    assert "mcp.example.com/sse" in message


def test_error_name_strips_url_credentials_from_default_name() -> None:
    """HTTP server names default to the connection URL, which can embed credentials."""
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})

    assert "s3cr3t_pw" in server.name  # the raw name still carries them
    _assert_url_credentials_redacted(server._error_name)


def test_error_name_leaves_explicit_names_untouched() -> None:
    server = MCPServerSse(params={"url": _CREDENTIALED_URL}, name="my server")

    assert server._error_name == "my server"


def test_connect_http_error_redacts_url_credentials() -> None:
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(503, request=request)
    )

    with pytest.raises(UserError) as exc_info:
        server._raise_user_error_for_http_error(http_error)

    _assert_url_credentials_redacted(str(exc_info.value))


@pytest.mark.asyncio
async def test_list_tools_http_error_redacts_url_credentials() -> None:
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(500, request=request)
    )

    with patch.object(server, "_run_with_retries", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.list_tools(None, None)

    _assert_url_credentials_redacted(str(exc_info.value))


@pytest.mark.asyncio
async def test_call_tool_connect_error_redacts_url_credentials() -> None:
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()

    with patch.object(server, "_run_with_retries", side_effect=httpx.ConnectError("down")):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("some_tool", {})

    _assert_url_credentials_redacted(str(exc_info.value))


@pytest.mark.asyncio
async def test_invoke_mcp_tool_error_redacts_url_credentials_in_exception() -> None:
    """The raised AgentsException must not leak credentials the sibling log call redacts."""
    from mcp.types import Tool as MCPTool

    from agents.exceptions import AgentsException
    from agents.mcp import MCPUtil
    from agents.run_context import RunContextWrapper

    from .helpers import FakeMCPServer

    server = FakeMCPServer(server_name=f"sse: {_CREDENTIALED_URL}")
    server.add_tool("test_tool", {})

    async def boom(*args, **kwargs):
        raise ValueError("upstream exploded")

    server.call_tool = boom  # type: ignore[method-assign]

    with pytest.raises(AgentsException) as exc_info:
        await MCPUtil.invoke_mcp_tool(
            server,
            MCPTool(name="test_tool", inputSchema={}),
            RunContextWrapper(context=None),
            "",
        )

    _assert_url_credentials_redacted(str(exc_info.value))
