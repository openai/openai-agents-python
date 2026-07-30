import builtins
import logging
import sys
import traceback
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents import Agent, _debug
from agents.exceptions import UserError
from agents.mcp.server import (
    MCPServerSse,
    MCPServerStreamableHttp,
    _MCPServerWithClientSession,
)
from agents.run_context import RunContextWrapper

# Handle Python version compatibility for ExceptionGroups
if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup
else:
    BaseExceptionGroup = builtins.BaseExceptionGroup


_CREDENTIALED_URL = (
    "https://user:s3cr3t_pw@mcp.example.com/sse?api_key=SECRET_QS_KEY#SECRET_FRAGMENT"
)
_URL_SECRETS = ("user", "s3cr3t_pw", "SECRET_QS_KEY", "SECRET_FRAGMENT")
_SAFE_URL = "https://mcp.example.com/sse"


def _assert_url_credentials_hidden(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    for secret in _URL_SECRETS:
        assert secret not in str(error)
        assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def _assert_not_retained_in_traceback_locals(error: BaseException, sensitive_value: object) -> None:
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/src/agents/mcp/server.py"):
            assert all(value is not sensitive_value for value in current.tb_frame.f_locals.values())
        current = current.tb_next


def _assert_url_credentials_hidden_from_log_record(record: logging.LogRecord) -> None:
    rendered = logging.Formatter("%(levelname)s %(message)s").format(record)
    attached_values = repr(
        {
            "msg": record.msg,
            "args": record.args,
            "exc_info": record.exc_info,
            "exc_text": record.exc_text,
            "extra": record.__dict__,
        }
    )
    for secret in _URL_SECRETS:
        assert secret not in rendered
        assert secret not in attached_values


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
@pytest.mark.parametrize(
    ("url", "retains_cause"),
    [
        ("http://fake-mcp-server", True),
        (_CREDENTIALED_URL, False),
    ],
)
async def test_call_tool_nested_exception_group_mapping(url: str, retains_cause: bool):
    """
    Regression test ensuring that nested ExceptionGroups containing HTTP errors
    are recursively extracted and mapped to a UserError in call_tool().
    """
    # 1. Initialize the server with mock streamable parameters
    server = MCPServerStreamableHttp(params={"url": url})

    # 2. Simulate an active connection by mocking the session object
    server.session = MagicMock()

    # 3. Construct a nested ExceptionGroup hierarchy containing a connection error
    request = httpx.Request("POST", url)
    http_error = httpx.ConnectError("Network unreachable", request=request)
    inner_group = BaseExceptionGroup("inner_failures", [http_error])
    outer_group = BaseExceptionGroup("outer_failures", [inner_group])

    # 4 & 5. Mock the internal retry handler to raise the nested group, and assert UserError
    with patch.object(server, "_call_tool_with_isolated_retry", side_effect=outer_group):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool(tool_name="test_tool", arguments={})

    # 6. Verify that the user-facing message is mapped correctly based on the root cause
    assert "Connection lost" in str(exc_info.value)
    if retains_cause:
        assert exc_info.value.__cause__ is http_error
    else:
        assert "mcp.example.com/sse" in str(exc_info.value)
        _assert_url_credentials_hidden(exc_info.value)
        _assert_not_retained_in_traceback_locals(exc_info.value, http_error)


def _mixed_request_error_group(
    later_url: str,
) -> tuple[BaseExceptionGroup, httpx.ReadError, httpx.ConnectError]:
    safe_error = httpx.ReadError(
        "safe read failed",
        request=httpx.Request("GET", _SAFE_URL),
    )
    later_error = httpx.ConnectError(
        "later connection failed",
        request=httpx.Request("GET", later_url),
    )
    nested_group = BaseExceptionGroup("later failures", [later_error])
    return BaseExceptionGroup("mixed failures", [safe_error, nested_group]), safe_error, later_error


@pytest.mark.asyncio
async def test_connect_checks_every_request_error_before_preserving_exception_group():
    server = MCPServerSse(params={"url": _SAFE_URL})
    error_group, _, unsafe_error = _mixed_request_error_group(_CREDENTIALED_URL)

    with patch.object(server, "create_streams", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    assert "Could not reach the server" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, unsafe_error)


@pytest.mark.asyncio
async def test_call_tool_checks_every_request_error_before_preserving_exception_group():
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    server.session = MagicMock()
    server.max_retry_attempts = 0
    error_group, _, unsafe_error = _mixed_request_error_group(_CREDENTIALED_URL)

    with patch.object(server, "_call_tool_with_isolated_retry", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("test_tool", {})

    assert "Connection lost" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, unsafe_error)


@pytest.mark.asyncio
async def test_connect_preserves_exception_group_when_every_request_error_is_safe():
    server = MCPServerSse(params={"url": _SAFE_URL})
    error_group, _, _ = _mixed_request_error_group(_SAFE_URL)

    with patch.object(server, "create_streams", side_effect=error_group):
        with pytest.raises(BaseExceptionGroup) as exc_info:
            await server.connect()

    assert exc_info.value is error_group


@pytest.mark.parametrize("server_type", [MCPServerSse, MCPServerStreamableHttp])
def test_error_name_sanitizes_url_derived_names_without_changing_runtime_name(server_type):
    server = server_type(params={"url": _CREDENTIALED_URL})

    assert server.name.endswith(_CREDENTIALED_URL)
    assert server._error_name.endswith("https://mcp.example.com/sse")

    explicitly_named = server_type(params={"url": _CREDENTIALED_URL}, name="safe server")
    assert explicitly_named._error_name == "safe server"


@pytest.mark.asyncio
async def test_connect_http_error_hides_url_credentials_from_exception_graph():
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(503, request=request),
    )

    with patch.object(server, "create_streams", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "HTTP error 503 (Service Unavailable)" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, http_error)


@pytest.mark.asyncio
async def test_list_tools_http_error_hides_url_credentials_from_exception_graph():
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(500, request=request)
    )

    with patch.object(server, "_run_with_retries", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.list_tools(None, None)

    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "HTTP error 500" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)


@pytest.mark.asyncio
async def test_list_tools_http_error_hides_redirect_history_url_credentials():
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    final_request = httpx.Request("GET", "https://mcp.example.com/final")
    redirect_response = httpx.Response(
        302,
        request=httpx.Request("GET", _CREDENTIALED_URL),
    )
    response = httpx.Response(
        500,
        request=final_request,
        history=[redirect_response],
    )
    http_error = httpx.HTTPStatusError(
        "boom",
        request=final_request,
        response=response,
    )

    with patch.object(server, "_run_with_retries", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.list_tools(None, None)

    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "HTTP error 500" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)


@pytest.mark.asyncio
async def test_list_tools_http_error_hides_current_redirect_location_credentials():
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    request = httpx.Request("GET", _SAFE_URL)
    response = httpx.Response(
        302,
        request=request,
        headers={"location": _CREDENTIALED_URL},
    )
    with pytest.raises(httpx.HTTPStatusError) as http_error_info:
        response.raise_for_status()
    http_error = http_error_info.value

    with patch.object(server, "_run_with_retries", side_effect=http_error):
        with pytest.raises(UserError) as exc_info:
            await server.list_tools(None, None)

    assert "HTTP error 302" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, http_error)


@pytest.mark.asyncio
async def test_call_tool_connect_error_hides_url_credentials_from_exception_graph():
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    server.session = MagicMock()
    request = httpx.Request("POST", _CREDENTIALED_URL)
    connect_error = httpx.ConnectError("down", request=request)

    with patch.object(server, "_run_with_retries", side_effect=connect_error):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("safe_tool", {})

    assert "safe_tool" in str(exc_info.value)
    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "Connection lost" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_type", [MCPServerSse, MCPServerStreamableHttp])
@pytest.mark.parametrize(
    ("url", "maps_to_user_error"),
    [
        (_SAFE_URL, False),
        (_CREDENTIALED_URL, True),
    ],
)
async def test_list_tools_direct_timeout_only_maps_credentialed_urls(
    server_type, url: str, maps_to_user_error: bool
):
    server = server_type(params={"url": url})
    server.session = MagicMock()
    timeout_error = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("GET", url),
    )

    with patch.object(server, "_run_with_retries", side_effect=timeout_error):
        if maps_to_user_error:
            with pytest.raises(UserError) as user_error_info:
                await server.list_tools(None, None)

            assert "Connection timeout" in str(user_error_info.value)
            _assert_url_credentials_hidden(user_error_info.value)
        else:
            with pytest.raises(httpx.ReadTimeout) as timeout_info:
                await server.list_tools(None, None)

            assert timeout_info.value is timeout_error


@pytest.mark.asyncio
@pytest.mark.parametrize("server_type", [MCPServerSse, MCPServerStreamableHttp])
@pytest.mark.parametrize(
    ("url", "maps_to_user_error"),
    [
        (_SAFE_URL, False),
        (_CREDENTIALED_URL, True),
    ],
)
async def test_call_tool_direct_timeout_only_maps_credentialed_urls(
    server_type, url: str, maps_to_user_error: bool
):
    server = server_type(params={"url": url})
    server.session = MagicMock()
    server.max_retry_attempts = 0
    timeout_error = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("POST", url),
    )
    retry_method = (
        "_call_tool_with_isolated_retry"
        if server_type is MCPServerStreamableHttp
        else "_run_with_retries"
    )

    with patch.object(server, retry_method, side_effect=timeout_error):
        if maps_to_user_error:
            with pytest.raises(UserError) as user_error_info:
                await server.call_tool("safe_tool", {})

            assert "Connection timeout" in str(user_error_info.value)
            _assert_url_credentials_hidden(user_error_info.value)
        else:
            with pytest.raises(httpx.ReadTimeout) as timeout_info:
                await server.call_tool("safe_tool", {})

            assert timeout_info.value is timeout_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    ],
)
@pytest.mark.parametrize(
    ("url", "maps_to_user_error"),
    [
        (_SAFE_URL, False),
        (_CREDENTIALED_URL, True),
    ],
)
async def test_list_tools_request_errors_only_map_credentialed_urls(
    error_type, url: str, maps_to_user_error: bool
):
    server = MCPServerSse(params={"url": url})
    server.session = MagicMock()
    request_error = error_type(
        "request failed",
        request=httpx.Request("GET", url),
    )

    with patch.object(server, "_run_with_retries", side_effect=request_error):
        if maps_to_user_error:
            with pytest.raises(UserError) as user_error_info:
                await server.list_tools(None, None)

            assert "Request failed" in str(user_error_info.value)
            _assert_url_credentials_hidden(user_error_info.value)
            _assert_not_retained_in_traceback_locals(
                user_error_info.value,
                request_error,
            )
        else:
            with pytest.raises(error_type) as request_error_info:
                await server.list_tools(None, None)

            assert request_error_info.value is request_error


@pytest.mark.asyncio
@pytest.mark.parametrize("server_type", [MCPServerSse, MCPServerStreamableHttp])
@pytest.mark.parametrize(
    ("url", "maps_to_user_error"),
    [
        (_SAFE_URL, False),
        (_CREDENTIALED_URL, True),
    ],
)
async def test_call_tool_request_error_only_maps_credentialed_urls(
    server_type, url: str, maps_to_user_error: bool
):
    server = server_type(params={"url": url})
    server.session = MagicMock()
    server.max_retry_attempts = 0
    request_error = httpx.ReadError(
        "request failed",
        request=httpx.Request("POST", url),
    )
    retry_method = (
        "_call_tool_with_isolated_retry"
        if server_type is MCPServerStreamableHttp
        else "_run_with_retries"
    )

    with patch.object(server, retry_method, side_effect=request_error):
        if maps_to_user_error:
            with pytest.raises(UserError) as user_error_info:
                await server.call_tool("safe_tool", {})

            assert "Request failed" in str(user_error_info.value)
            _assert_url_credentials_hidden(user_error_info.value)
            _assert_not_retained_in_traceback_locals(
                user_error_info.value,
                request_error,
            )
        else:
            with pytest.raises(httpx.ReadError) as request_error_info:
                await server.call_tool("safe_tool", {})

            assert request_error_info.value is request_error


@pytest.mark.asyncio
async def test_failed_connection_cleanup_hides_url_credentials_from_exception_graph():
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(502, request=request)
    )
    cleanup_group = BaseExceptionGroup("cleanup failed", [http_error])

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "HTTP error 502" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, http_error)


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted", [True, False])
@pytest.mark.parametrize(
    ("url", "safe_to_attach"),
    [
        (_SAFE_URL, True),
        (_CREDENTIALED_URL, False),
    ],
)
async def test_normal_cleanup_only_logs_safe_transport_exceptions(
    monkeypatch,
    caplog,
    redacted: bool,
    url: str,
    safe_to_attach: bool,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    server = MCPServerSse(params={"url": url})
    server.session = MagicMock()
    timeout_error = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("GET", url),
    )
    cleanup_group = BaseExceptionGroup("cleanup failed", [timeout_error])

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    if not redacted and safe_to_attach:
        assert record.exc_info is not None
        assert record.exc_info[1] is timeout_error
    else:
        assert record.exc_info is None

    if not safe_to_attach:
        _assert_url_credentials_hidden_from_log_record(record)
