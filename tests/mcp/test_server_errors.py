import asyncio
import builtins
import os
import sys
import traceback
from unittest.mock import MagicMock, patch

import httpx
import pytest

import agents._debug as _debug
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
async def test_call_tool_nested_exception_group_mapping(monkeypatch: pytest.MonkeyPatch):
    """
    Regression test ensuring that nested ExceptionGroups containing HTTP errors
    are recursively extracted and mapped to a UserError in call_tool().
    """
    # The cause is only chained when tool-data logging is enabled, because an httpx error
    # keeps the request URL (which can embed credentials) reachable from the raised error.
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
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

    error = server._build_user_error_for_http_error(http_error)

    _assert_url_credentials_redacted(str(error))


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

    _assert_no_credentials_anywhere(exc_info.value)


@pytest.mark.asyncio
async def test_call_tool_connect_error_redacts_url_credentials() -> None:
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()

    with patch.object(server, "_run_with_retries", side_effect=httpx.ConnectError("down")):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("some_tool", {})

    _assert_no_credentials_anywhere(exc_info.value)


def _assert_no_credentials_anywhere(error: BaseException) -> None:
    """The credentials must be absent from the message, the whole chain, and the traceback."""
    _assert_url_credentials_redacted(str(error))

    # Recursive __cause__ / __context__ walk, including objects the exceptions hold on to.
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert "s3cr3t_pw" not in str(current)
        assert "SECRET_QS_KEY" not in str(current)
        request = getattr(current, "request", None)
        if request is not None:
            assert "s3cr3t_pw" not in str(getattr(request, "url", ""))
            assert "SECRET_QS_KEY" not in str(getattr(request, "url", ""))
        pending.extend([current.__cause__, current.__context__])

    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert "s3cr3t_pw" not in rendered
    assert "SECRET_QS_KEY" not in rendered

    _assert_no_credentials_in_frames(error)


def _assert_no_credentials_in_frames(error: BaseException) -> None:
    """Telemetry that captures frame locals must not recover the transport error.

    Only frames inside agents/mcp are checked: the caller's own frame legitimately still
    holds the exception it raised.
    """
    tb = error.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if f"agents{os.sep}mcp{os.sep}" in filename:
            for local_name, value in tb.tb_frame.f_locals.items():
                where = f"{os.path.basename(filename)}:{local_name}"
                candidates: list[BaseException] = []
                if isinstance(value, BaseException):
                    candidates.append(value)
                elif isinstance(value, asyncio.Task) and value.done():
                    # A completed task keeps its exception, and repr(task) renders it.
                    assert "s3cr3t_pw" not in repr(value), f"leaked via local {where}"
                    if not value.cancelled():
                        task_exc = value.exception()
                        if task_exc is not None:
                            candidates.append(task_exc)
                for candidate in candidates:
                    assert "s3cr3t_pw" not in str(candidate), f"leaked via local {where}"
                    request = getattr(candidate, "request", None)
                    if request is not None:
                        url = str(getattr(request, "url", ""))
                        assert "s3cr3t_pw" not in url, f"leaked via local {where}"
                        assert "SECRET_QS_KEY" not in url, f"leaked via local {where}"
        tb = tb.tb_next


@pytest.mark.asyncio
async def test_call_tool_does_not_retain_credentialed_cause_by_default() -> None:
    """The httpx error keeps the credentialed request URL, so it must not stay reachable."""
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(503, request=request)
    )

    with patch.object(server, "_run_with_retries", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("some_tool", {})

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_credentials_anywhere(error)


@pytest.mark.asyncio
async def test_connect_failure_does_not_retain_credentialed_cause() -> None:
    """connect() wraps transport failures; the httpx error must not survive on the chain."""
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(401, request=request)
    )

    with patch.object(server, "create_streams", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_credentials_anywhere(error)


@pytest.mark.asyncio
async def test_call_tool_chains_cause_when_tool_logging_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    connect_error = httpx.ConnectError("down")

    with patch.object(server, "_run_with_retries", side_effect=connect_error):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("some_tool", {})

    assert exc_info.value.__cause__ is connect_error


@pytest.mark.asyncio
async def test_invoke_mcp_tool_does_not_inspect_exception_while_redacting() -> None:
    """A custom MCPServer controls its exception type, so redacted mode must not touch it."""
    from mcp.types import Tool as MCPTool

    from agents.exceptions import AgentsException
    from agents.mcp import MCPUtil
    from agents.run_context import RunContextWrapper

    from .helpers import FakeMCPServer

    class _HostileException(Exception):
        """Reading the message must not happen, and the type name itself is a secret."""

        def __str__(self) -> str:
            raise AssertionError("redacted error inspected __str__")

        def __repr__(self) -> str:
            raise AssertionError("redacted error inspected __repr__")

    # A dynamically created exception type can carry a secret in its name, so the type name
    # is not safe operational metadata either.
    _HostileException.__name__ = "SECRET_TYPE_NAME_123"
    _HostileException.__qualname__ = "SECRET_TYPE_NAME_123"

    server = FakeMCPServer(server_name="hostile server")
    server.add_tool("test_tool", {})

    async def boom(*args, **kwargs):
        raise _HostileException()

    server.call_tool = boom  # type: ignore[method-assign]

    with pytest.raises(AgentsException) as exc_info:
        await MCPUtil.invoke_mcp_tool(
            server,
            MCPTool(name="test_tool", inputSchema={}),
            RunContextWrapper(context=None),
            "",
        )

    message = str(exc_info.value)
    assert "Error invoking MCP tool test_tool" in message
    assert "SECRET_TYPE_NAME_123" not in message
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


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
        # The transport error text itself carries the credentialed URL, which is exactly
        # what the generic wrapper used to copy into the caller-visible message.
        raise ValueError(f"connection to {_CREDENTIALED_URL} failed")

    server.call_tool = boom  # type: ignore[method-assign]

    with pytest.raises(AgentsException) as exc_info:
        await MCPUtil.invoke_mcp_tool(
            server,
            MCPTool(name="test_tool", inputSchema={}),
            RunContextWrapper(context=None),
            "",
        )

    error = exc_info.value
    message = str(error)
    assert "s3cr3t_pw" not in message
    assert "SECRET_QS_KEY" not in message
    # This message reaches the model through the default failure formatter, so while tool
    # data is redacted it carries no server name at all.
    assert "mcp.example.com" not in message
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_no_credentials_in_frames(error)
