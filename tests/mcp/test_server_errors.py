import asyncio
import builtins
import logging
import sys
import traceback
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents import Agent, _debug
from agents.exceptions import UserError
from agents.mcp._compat import MCP_V2
from agents.mcp.server import (
    MCPServerSse,
    MCPServerStreamableHttp,
    _client_session_read_timeout,
    _MCPServerWithClientSession,
)
from agents.run_context import RunContextWrapper

from .model_compat import ListPromptsResult, ListToolsResult

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
_PROMPT_RESOURCE_OPERATIONS = [
    ("list_prompts", (), "list prompts"),
    ("get_prompt", ("safe_prompt", None), "get prompt"),
    ("list_resources", (None,), "list resources"),
    ("list_resource_templates", (None,), "list resource templates"),
    ("read_resource", ("file:///safe.txt",), "read resource"),
]


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


def _assert_not_retained_in_exception_graph(
    error: BaseException,
    sensitive_value: object,
) -> None:
    pending: list[object] = [error]
    seen: set[int] = set()

    while pending:
        value = pending.pop()
        assert value is not sensitive_value
        if id(value) in seen:
            continue
        seen.add(id(value))

        if isinstance(value, BaseException):
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.extend(getattr(value, "__notes__", ()))
            pending.append(value.__dict__)
            if isinstance(value, BaseExceptionGroup):
                pending.extend(value.exceptions)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(value)


def _assert_url_credentials_hidden_from_traceback_locals(error: BaseException) -> None:
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/src/agents/mcp/server.py"):
            attached_values = repr(tuple(current.tb_frame.f_locals.values()))
            for secret in _URL_SECRETS:
                assert secret not in attached_values
        current = current.tb_next


def _assert_text_hidden_from_server_traceback_locals(
    error: BaseException,
    sensitive_text: str,
) -> None:
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/src/agents/mcp/server.py"):
            attached_values = repr(tuple(current.tb_frame.f_locals.values()))
            assert sensitive_text not in attached_values
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


def _assert_not_retained_in_log_record(
    record: logging.LogRecord,
    sensitive_value: object,
) -> None:
    pending: list[object] = [record.__dict__]
    seen: set[int] = set()

    while pending:
        value = pending.pop()
        assert value is not sensitive_value
        if id(value) in seen:
            continue
        seen.add(id(value))

        if isinstance(value, BaseException):
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.extend(getattr(value, "__notes__", ()))
            pending.append(value.__dict__)
            if isinstance(value, BaseExceptionGroup):
                pending.extend(value.exceptions)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(value)


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


@pytest.mark.parametrize("timeout_seconds", [None, 0, 0.0])
def test_client_session_read_timeout_treats_zero_as_disabled(
    timeout_seconds: float | None,
) -> None:
    assert _client_session_read_timeout(timeout_seconds) is None


@pytest.mark.parametrize("timeout_seconds", [0.000001, 2.5])
def test_client_session_read_timeout_preserves_positive_value(timeout_seconds: float) -> None:
    expected = timeout_seconds if MCP_V2 else timedelta(seconds=timeout_seconds)
    assert _client_session_read_timeout(timeout_seconds) == expected


@pytest.mark.parametrize(
    ("timeout_seconds", "error_type"),
    [
        (True, TypeError),
        ("5", TypeError),
        (-1, ValueError),
        (-0.5, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (5e-7, ValueError),
        (1e20, ValueError),
        (10**400, ValueError),
    ],
)
def test_server_rejects_unsupported_client_session_read_timeout_at_construction(
    timeout_seconds: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="client_session_timeout_seconds"):
        MCPServerSse(
            params={"url": "https://mcp.example.com/sse"},
            client_session_timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_server_errors_cause_error_and_cleanup_called():
    server = CrashingClientSessionServer()

    with pytest.raises(ValueError):
        await server.connect()

    assert server.cleanup_called


@pytest.mark.asyncio
async def test_server_revalidates_mutated_timeout_before_creating_streams() -> None:
    server = CrashingClientSessionServer()
    server.client_session_timeout_seconds = 5e-7

    with pytest.raises(ValueError, match="at least one microsecond"):
        await server.connect()

    assert server.cleanup_called is False


@pytest.mark.asyncio
async def test_isolated_session_revalidates_mutated_timeout_before_creating_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = MCPServerStreamableHttp(params={"url": "https://mcp.example.com/mcp"})
    create_streams = MagicMock()
    monkeypatch.setattr(server, "create_streams", create_streams)
    server.client_session_timeout_seconds = 5e-7

    with pytest.raises(ValueError, match="at least one microsecond"):
        async with server._isolated_client_session():
            raise AssertionError("context body should not run")

    create_streams.assert_not_called()


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
    ("method_name", "args", "operation"),
    _PROMPT_RESOURCE_OPERATIONS,
)
@pytest.mark.parametrize("redacted", [True, False])
async def test_prompt_and_resource_request_errors_hide_url_credentials(
    monkeypatch,
    caplog,
    method_name: str,
    args: tuple[object, ...],
    operation: str,
    redacted: bool,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    server = MCPServerStreamableHttp(params={"url": _CREDENTIALED_URL})
    request_error = httpx.ReadError(
        "request failed",
        request=httpx.Request("POST", _CREDENTIALED_URL),
    )
    session = MagicMock()
    setattr(session, method_name, AsyncMock(side_effect=request_error))
    server.session = session

    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        with pytest.raises(UserError) as user_error_info:
            await getattr(server, method_name)(*args)

    assert f"Failed to {operation}" in str(user_error_info.value)
    assert "mcp.example.com/sse" in str(user_error_info.value)
    assert "Request failed" in str(user_error_info.value)
    assert not hasattr(user_error_info.value, "request")
    _assert_url_credentials_hidden(user_error_info.value)
    _assert_not_retained_in_traceback_locals(user_error_info.value, request_error)
    _assert_url_credentials_hidden_from_traceback_locals(user_error_info.value)
    assert not [record for record in caplog.records if record.name == "openai.agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args", "_operation"),
    _PROMPT_RESOURCE_OPERATIONS,
)
async def test_prompt_and_resource_request_errors_hide_attached_request_data(
    method_name: str,
    args: tuple[object, ...],
    _operation: str,
):
    session_secret = "SECRET_MCP_SESSION_ID"
    body_secret = "SECRET_REQUEST_BODY"
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    request_error = httpx.ReadError(
        "request failed",
        request=httpx.Request(
            "POST",
            _SAFE_URL,
            headers={"mcp-session-id": session_secret},
            content=body_secret,
        ),
    )
    session = MagicMock()
    setattr(session, method_name, AsyncMock(side_effect=request_error))
    server.session = session

    with pytest.raises(UserError) as user_error_info:
        await getattr(server, method_name)(*args)

    rendered = "".join(traceback.format_exception(user_error_info.value))
    assert session_secret not in rendered
    assert body_secret not in rendered
    assert user_error_info.value.__cause__ is None
    assert user_error_info.value.__context__ is None
    _assert_not_retained_in_traceback_locals(user_error_info.value, request_error)
    _assert_not_retained_in_exception_graph(user_error_info.value, request_error)


@pytest.mark.asyncio
async def test_prompt_http_status_errors_hide_attached_response_data():
    request_body_secret = "SECRET_REQUEST_BODY"
    response_header_secret = "SECRET_RESPONSE_COOKIE"
    response_body_secret = "SECRET_RESPONSE_BODY"
    history_body_secret = "SECRET_HISTORY_BODY"
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    request = httpx.Request("POST", _SAFE_URL, content=request_body_secret)
    history_request = httpx.Request("POST", _SAFE_URL)
    history_response = httpx.Response(
        307,
        request=history_request,
        headers={"set-cookie": history_body_secret},
        content=history_body_secret,
    )
    response = httpx.Response(
        503,
        request=request,
        headers={"set-cookie": response_header_secret},
        content=response_body_secret,
        history=[history_response],
    )
    http_error = httpx.HTTPStatusError("boom", request=request, response=response)
    session = MagicMock()
    session.list_prompts = AsyncMock(side_effect=http_error)
    server.session = session

    with pytest.raises(UserError) as user_error_info:
        await server.list_prompts()

    rendered = "".join(traceback.format_exception(user_error_info.value))
    for secret in (
        request_body_secret,
        response_header_secret,
        response_body_secret,
        history_body_secret,
    ):
        assert secret not in rendered
    assert user_error_info.value.__cause__ is None
    assert user_error_info.value.__context__ is None
    _assert_not_retained_in_traceback_locals(user_error_info.value, http_error)
    _assert_not_retained_in_exception_graph(user_error_info.value, http_error)


@pytest.mark.asyncio
async def test_prompt_request_http_status_hides_url_credentials():
    server = MCPServerStreamableHttp(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(503, request=request),
    )
    session = MagicMock()
    session.list_prompts = AsyncMock(side_effect=http_error)
    server.session = session

    with pytest.raises(UserError) as user_error_info:
        await server.list_prompts()

    assert "HTTP error 503" in str(user_error_info.value)
    _assert_url_credentials_hidden(user_error_info.value)
    _assert_not_retained_in_traceback_locals(user_error_info.value, http_error)
    _assert_url_credentials_hidden_from_traceback_locals(user_error_info.value)


def _paginated_list_result(
    method_name: str,
    next_cursor: str,
) -> ListToolsResult | ListPromptsResult:
    if method_name == "list_tools":
        return ListToolsResult(tools=[], nextCursor=next_cursor)
    return ListPromptsResult(prompts=[], nextCursor=next_cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["list_tools", "list_prompts"])
async def test_paginated_list_failure_does_not_retain_opaque_cursor(method_name: str):
    cursor = "SECRET_OPAQUE_CURSOR"
    failure_message = "SECRET_CONTINUATION_FAILURE"
    continuation_error = RuntimeError(failure_message)
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    session = MagicMock()
    setattr(
        session,
        method_name,
        AsyncMock(
            side_effect=[
                _paginated_list_result(method_name, cursor),
                continuation_error,
            ]
        ),
    )
    server.session = session
    server.max_retry_attempts = 0

    with pytest.raises(UserError) as user_error_info:
        await getattr(server, method_name)()

    rendered = "".join(traceback.format_exception(user_error_info.value))
    assert "Request failed" in str(user_error_info.value)
    assert cursor not in rendered
    assert failure_message not in rendered
    assert user_error_info.value.__cause__ is None
    assert user_error_info.value.__context__ is None
    _assert_not_retained_in_exception_graph(user_error_info.value, continuation_error)
    _assert_text_hidden_from_server_traceback_locals(user_error_info.value, cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["list_tools", "list_prompts"])
async def test_paginated_list_cycle_does_not_retain_opaque_cursor(method_name: str):
    cursor = "SECRET_OPAQUE_CURSOR"
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    session = MagicMock()
    setattr(
        session,
        method_name,
        AsyncMock(
            side_effect=[
                _paginated_list_result(method_name, cursor),
                _paginated_list_result(method_name, cursor),
            ]
        ),
    )
    server.session = session
    server.max_retry_attempts = 0

    with pytest.raises(UserError, match=f"repeated cursor while listing {method_name[5:]}") as info:
        await getattr(server, method_name)()

    assert cursor not in "".join(traceback.format_exception(info.value))
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    _assert_text_hidden_from_server_traceback_locals(info.value, cursor)
    if method_name == "list_tools":
        assert server.cached_tools is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["list_tools", "list_prompts"])
async def test_paginated_list_cancellation_preserves_control_flow_without_cursor(
    method_name: str,
):
    cursor = "SECRET_OPAQUE_CURSOR"
    cancellation = asyncio.CancelledError(cursor)
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    session = MagicMock()
    setattr(
        session,
        method_name,
        AsyncMock(
            side_effect=[
                _paginated_list_result(method_name, cursor),
                cancellation,
            ]
        ),
    )
    server.session = session
    server.max_retry_attempts = 0

    with pytest.raises(asyncio.CancelledError) as cancellation_info:
        await getattr(server, method_name)()

    assert str(cancellation_info.value) == ""
    assert cancellation_info.value.__cause__ is None
    assert cancellation_info.value.__context__ is None
    _assert_not_retained_in_exception_graph(cancellation_info.value, cancellation)
    _assert_text_hidden_from_server_traceback_locals(cancellation_info.value, cursor)


@pytest.mark.asyncio
async def test_paginated_tools_clear_cursor_before_filter_failure():
    cursor = "SECRET_OPAQUE_CURSOR"
    server = MCPServerStreamableHttp(
        params={"url": _SAFE_URL},
        tool_filter=lambda context, tool: True,
    )
    session = MagicMock()
    session.list_tools = AsyncMock(
        side_effect=[
            ListToolsResult(tools=[], nextCursor=cursor),
            ListToolsResult(tools=[]),
        ]
    )
    server.session = session

    with pytest.raises(UserError, match="run_context and agent are required") as error_info:
        await server.list_tools()

    assert cursor not in "".join(traceback.format_exception(error_info.value))
    _assert_text_hidden_from_server_traceback_locals(error_info.value, cursor)


@pytest.mark.asyncio
async def test_resource_request_nested_group_replaces_ordinary_siblings_safely():
    server = MCPServerStreamableHttp(params={"url": _CREDENTIALED_URL})
    request_error = httpx.ConnectError(
        "connection failed",
        request=httpx.Request("GET", _CREDENTIALED_URL),
    )

    ordinary_error = ValueError("ordinary sibling failure", request_error)
    ordinary_error.__notes__ = [_CREDENTIALED_URL]
    ordinary_error.unsafe_request = request_error  # type: ignore[attr-defined]
    error_group = BaseExceptionGroup(
        "request failed",
        [
            ordinary_error,
            BaseExceptionGroup("transport failed", [request_error]),
        ],
    )
    session = MagicMock()
    session.read_resource = AsyncMock(side_effect=error_group)
    server.session = session

    with pytest.raises(BaseExceptionGroup) as error_group_info:
        await server.read_resource("file:///safe.txt")

    propagated_group = error_group_info.value
    assert len(propagated_group.exceptions) == 2
    propagated_transport_error, propagated_error = propagated_group.exceptions
    assert isinstance(propagated_transport_error, UserError)
    assert "Failed to read resource" in str(propagated_transport_error)
    assert "Connection lost" in str(propagated_transport_error)
    assert propagated_transport_error.__cause__ is None
    assert propagated_transport_error.__context__ is None
    assert isinstance(propagated_error, RuntimeError)
    assert str(propagated_error) == "An additional error occurred during the MCP request."
    assert id(propagated_error) != id(ordinary_error)
    _assert_url_credentials_hidden(propagated_group)
    _assert_not_retained_in_traceback_locals(propagated_group, error_group)
    _assert_not_retained_in_traceback_locals(propagated_group, request_error)
    _assert_not_retained_in_exception_graph(propagated_group, ordinary_error)
    _assert_not_retained_in_exception_graph(propagated_group, request_error)
    _assert_url_credentials_hidden_from_traceback_locals(propagated_group)


@pytest.mark.asyncio
async def test_resource_request_mixed_group_preserves_cancellation():
    server = MCPServerStreamableHttp(params={"url": _CREDENTIALED_URL})
    cancellation = asyncio.CancelledError("request cancelled")
    request_error = httpx.ConnectError(
        "connection failed",
        request=httpx.Request("GET", _CREDENTIALED_URL),
    )
    error_group: BaseExceptionGroup | None = None

    async def raise_mixed_group(uri: object) -> None:
        del uri
        nonlocal error_group
        error_group = BaseExceptionGroup(
            "request failed",
            [cancellation, request_error],
        )
        raise error_group

    session = MagicMock()
    session.read_resource = raise_mixed_group
    server.session = session

    with pytest.raises(BaseExceptionGroup) as error_group_info:
        await server.read_resource("file:///safe.txt")

    propagated_group = error_group_info.value
    assert len(propagated_group.exceptions) == 2
    propagated_transport_error, propagated_cancellation = propagated_group.exceptions
    assert isinstance(propagated_transport_error, UserError)
    assert "Failed to read resource" in str(propagated_transport_error)
    assert "Connection lost" in str(propagated_transport_error)
    assert propagated_transport_error.__cause__ is None
    assert propagated_transport_error.__context__ is None
    assert isinstance(propagated_cancellation, asyncio.CancelledError)
    assert propagated_cancellation is not cancellation
    _assert_url_credentials_hidden(propagated_group)
    assert error_group is not None
    _assert_not_retained_in_traceback_locals(propagated_group, error_group)
    _assert_not_retained_in_traceback_locals(propagated_group, request_error)
    _assert_not_retained_in_exception_graph(propagated_group, cancellation)
    _assert_not_retained_in_exception_graph(propagated_group, request_error)
    _assert_url_credentials_hidden_from_traceback_locals(propagated_group)
    traceback_frames = []
    current = propagated_group.__traceback__
    while current is not None:
        traceback_frames.append(current.tb_frame)
        current = current.tb_next
    assert all(frame.f_code.co_name != "raise_mixed_group" for frame in traceback_frames)


@pytest.mark.asyncio
async def test_resource_request_sanitizes_safe_url_nested_group():
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    request_error = httpx.ConnectError(
        "connection failed",
        request=httpx.Request("GET", _SAFE_URL),
    )
    error_group = BaseExceptionGroup("request failed", [request_error])
    session = MagicMock()
    session.read_resource = AsyncMock(side_effect=error_group)
    server.session = session

    with pytest.raises(UserError) as user_error_info:
        await server.read_resource("file:///safe.txt")

    assert user_error_info.value.__cause__ is None
    assert user_error_info.value.__context__ is None
    _assert_not_retained_in_traceback_locals(user_error_info.value, error_group)
    _assert_not_retained_in_exception_graph(user_error_info.value, request_error)


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


def _transport_error_with_sensitive_attachment(
    attachment: str,
) -> tuple[httpx.ReadTimeout, object]:
    safe_outer_error = httpx.ReadTimeout(
        "outer timeout",
        request=httpx.Request("GET", _SAFE_URL),
    )
    if attachment == "http_context":
        http_context = httpx.ReadError(
            "inner read failed",
            request=httpx.Request("GET", _CREDENTIALED_URL),
        )
        sensitive_value: object = http_context
        safe_outer_error.__context__ = http_context
    elif attachment == "non_http_context":
        non_http_context = ValueError(_CREDENTIALED_URL)
        sensitive_value = non_http_context
        safe_outer_error.__context__ = non_http_context
    elif attachment == "note":
        sensitive_value = _CREDENTIALED_URL
        safe_outer_error.__dict__["__notes__"] = [sensitive_value]
    else:
        raise AssertionError(f"Unexpected attachment type: {attachment}")
    return safe_outer_error, sensitive_value


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
async def test_connect_group_hides_sensitive_transport_error_context():
    server = MCPServerSse(params={"url": _SAFE_URL})
    transport_error, sensitive_value = _transport_error_with_sensitive_attachment(
        "non_http_context"
    )
    error_group = BaseExceptionGroup("connection failed", [transport_error])

    with patch.object(server, "create_streams", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    assert "Connection timeout" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, transport_error)
    _assert_not_retained_in_exception_graph(exc_info.value, sensitive_value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, transport_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, sensitive_value)


@pytest.mark.asyncio
async def test_call_tool_group_hides_sensitive_transport_error_context():
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    server.session = MagicMock()
    server.max_retry_attempts = 0
    transport_error, sensitive_value = _transport_error_with_sensitive_attachment(
        "non_http_context"
    )
    error_group = BaseExceptionGroup("tool call failed", [transport_error])

    with patch.object(server, "_call_tool_with_isolated_retry", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("test_tool", {})

    assert "Connection timeout" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, transport_error)
    _assert_not_retained_in_exception_graph(exc_info.value, sensitive_value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, transport_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, sensitive_value)


@pytest.mark.asyncio
async def test_connect_group_checks_every_transport_error_attachment():
    server = MCPServerSse(params={"url": _SAFE_URL})
    error_group, _, later_error = _mixed_request_error_group(_SAFE_URL)
    sensitive_value = ValueError(_CREDENTIALED_URL)
    later_error.__context__ = sensitive_value

    with patch.object(server, "create_streams", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    assert "Could not reach the server" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, error_group)
    _assert_not_retained_in_exception_graph(exc_info.value, later_error)
    _assert_not_retained_in_exception_graph(exc_info.value, sensitive_value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, later_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, sensitive_value)


@pytest.mark.asyncio
async def test_call_tool_group_checks_every_transport_error_attachment():
    server = MCPServerStreamableHttp(params={"url": _SAFE_URL})
    server.session = MagicMock()
    server.max_retry_attempts = 0
    error_group, _, later_error = _mixed_request_error_group(_SAFE_URL)
    sensitive_value = ValueError(_CREDENTIALED_URL)
    later_error.__context__ = sensitive_value

    with patch.object(server, "_call_tool_with_isolated_retry", side_effect=error_group):
        with pytest.raises(UserError) as exc_info:
            await server.call_tool("test_tool", {})

    assert "Connection lost" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, error_group)
    _assert_not_retained_in_exception_graph(exc_info.value, later_error)
    _assert_not_retained_in_exception_graph(exc_info.value, sensitive_value)
    _assert_not_retained_in_traceback_locals(exc_info.value, error_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, later_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, sensitive_value)


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
@pytest.mark.parametrize("grouped", [False, True])
async def test_failed_connection_cleanup_hides_url_credentials_from_exception_graph(
    grouped: bool,
):
    server = MCPServerSse(params={"url": _CREDENTIALED_URL})
    request = httpx.Request("GET", _CREDENTIALED_URL)
    http_error = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(502, request=request)
    )
    cleanup_error: BaseException = http_error
    if grouped:
        cleanup_error = BaseExceptionGroup("cleanup failed", [http_error])

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "mcp.example.com/sse" in str(exc_info.value)
    assert "HTTP error 502" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, cleanup_error)
    _assert_not_retained_in_exception_graph(exc_info.value, http_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, http_error)


@pytest.mark.asyncio
async def test_connect_preserves_original_error_when_cleanup_has_safe_generic_request_error(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    connection_error = ValueError("original connection failure")
    cleanup_error = httpx.ReadError(
        "cleanup read failed",
        request=httpx.Request("GET", _SAFE_URL),
    )

    with (
        patch.object(server, "create_streams", side_effect=connection_error),
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        with pytest.raises(ValueError) as exc_info:
            await server.connect()

    assert exc_info.value is connection_error
    record = caplog.records[-1]
    assert record.exc_info is None
    assert record.exc_text is None
    _assert_not_retained_in_log_record(record, cleanup_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_connect_cleanup_mapped_error_omits_pending_connection_failure():
    server = MCPServerSse(params={"url": _SAFE_URL})
    connection_error = ValueError(_CREDENTIALED_URL)
    cleanup_error = httpx.ReadTimeout(
        "cleanup timed out",
        request=httpx.Request("GET", _SAFE_URL),
    )

    with (
        patch.object(server, "create_streams", side_effect=connection_error),
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
    ):
        with pytest.raises(UserError) as exc_info:
            await server.connect()

    assert "Connection timeout" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, connection_error)
    _assert_not_retained_in_exception_graph(exc_info.value, cleanup_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, connection_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, cleanup_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_normal_cleanup_hides_generic_request_error_context_from_log_record(
    monkeypatch,
    caplog,
    grouped: bool,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    request_error = httpx.ReadError(
        "cleanup read failed",
        request=httpx.Request("GET", _SAFE_URL),
    )
    sensitive_value = ValueError(_CREDENTIALED_URL)
    request_error.__context__ = sensitive_value
    cleanup_error: BaseException = request_error
    if grouped:
        cleanup_error = BaseExceptionGroup("cleanup failed", [request_error])

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    if grouped:
        assert record.exc_info is not None
        assert record.exc_info[1] is not cleanup_error
    else:
        assert record.exc_info is None
        assert record.exc_text is None
    _assert_not_retained_in_log_record(record, cleanup_error)
    _assert_not_retained_in_log_record(record, request_error)
    _assert_not_retained_in_log_record(record, sensitive_value)
    _assert_url_credentials_hidden_from_log_record(record)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["message", "reason_phrase"])
async def test_normal_cleanup_hides_transport_exception_payload_from_log_record(
    monkeypatch,
    caplog,
    payload: str,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    request = httpx.Request("GET", _SAFE_URL)
    if payload == "message":
        cleanup_error: Exception = httpx.ReadTimeout(_CREDENTIALED_URL, request=request)
    else:
        cleanup_error = httpx.HTTPStatusError(
            "cleanup failed",
            request=request,
            response=httpx.Response(
                502,
                request=request,
                extensions={"reason_phrase": _CREDENTIALED_URL.encode()},
            ),
        )

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.exc_info is None
    assert record.exc_text is None
    _assert_not_retained_in_log_record(record, cleanup_error)
    _assert_url_credentials_hidden_from_log_record(record)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment", ["http_context", "non_http_context", "note"])
async def test_failed_connection_cleanup_hides_sensitive_exception_attachments(
    attachment: str,
):
    server = MCPServerSse(params={"url": _SAFE_URL})
    cleanup_error, sensitive_value = _transport_error_with_sensitive_attachment(attachment)

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "Connection timeout" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, cleanup_error)
    _assert_not_retained_in_exception_graph(exc_info.value, sensitive_value)
    _assert_not_retained_in_traceback_locals(exc_info.value, cleanup_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, sensitive_value)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_failed_connection_cleanup_hides_sensitive_transport_error_message():
    server = MCPServerSse(params={"url": _SAFE_URL})
    cleanup_error = httpx.ReadTimeout(
        _CREDENTIALED_URL,
        request=httpx.Request("GET", _SAFE_URL),
    )

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "Connection timeout" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, cleanup_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, cleanup_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_failed_connection_cleanup_omits_untrusted_http_reason_phrase(grouped: bool):
    server = MCPServerSse(params={"url": _SAFE_URL})
    request = httpx.Request("GET", _SAFE_URL)
    http_error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(
            502,
            request=request,
            extensions={"reason_phrase": _CREDENTIALED_URL.encode()},
        ),
    )
    cleanup_error: BaseException = http_error
    if grouped:
        cleanup_error = BaseExceptionGroup("cleanup failed", [http_error])

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "HTTP error 502" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_exception_graph(exc_info.value, cleanup_error)
    _assert_not_retained_in_exception_graph(exc_info.value, http_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, cleanup_error)
    _assert_not_retained_in_traceback_locals(exc_info.value, http_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_failed_connection_cleanup_checks_every_nested_transport_error():
    server = MCPServerSse(params={"url": _SAFE_URL})
    cleanup_group, _, unsafe_error = _mixed_request_error_group(_CREDENTIALED_URL)

    with patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)):
        with pytest.raises(UserError) as exc_info:
            await server.cleanup()

    assert "Could not reach the server" in str(exc_info.value)
    _assert_url_credentials_hidden(exc_info.value)
    _assert_not_retained_in_traceback_locals(exc_info.value, cleanup_group)
    _assert_not_retained_in_traceback_locals(exc_info.value, unsafe_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted", [True, False])
@pytest.mark.parametrize("exception_shape", ["direct", "grouped", "nested_group"])
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
    exception_shape: str,
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
    cleanup_error: BaseException = timeout_error
    if exception_shape == "grouped":
        cleanup_error = BaseExceptionGroup("cleanup failed", [timeout_error])
    elif exception_shape == "nested_group":
        inner_group = BaseExceptionGroup("nested cleanup failed", [timeout_error])
        cleanup_error = BaseExceptionGroup("cleanup failed", [inner_group])

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    if not redacted and safe_to_attach and exception_shape == "nested_group":
        assert record.exc_info is not None
        assert record.levelno == logging.ERROR
        assert record.exc_info[1] is not cleanup_error
    else:
        assert record.exc_info is None
        assert record.exc_text is None

    _assert_not_retained_in_log_record(record, cleanup_error)
    _assert_not_retained_in_log_record(record, timeout_error)

    if not safe_to_attach:
        _assert_url_credentials_hidden_from_log_record(record)

    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted", [True, False])
@pytest.mark.parametrize("attachment", ["http_context", "non_http_context", "note"])
async def test_normal_cleanup_hides_sensitive_exception_attachments_from_log_record(
    monkeypatch,
    caplog,
    redacted: bool,
    attachment: str,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    cleanup_error, sensitive_value = _transport_error_with_sensitive_attachment(attachment)

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_error)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.exc_info is None
    assert record.exc_text is None
    _assert_not_retained_in_log_record(record, cleanup_error)
    _assert_not_retained_in_log_record(record, sensitive_value)
    _assert_url_credentials_hidden_from_log_record(record)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_normal_cleanup_sanitizes_safe_nested_group_diagnostics(monkeypatch, caplog):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    timeout_error = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("GET", _SAFE_URL),
    )
    ordinary_error = ValueError("ordinary sibling failure")
    cleanup_group = BaseExceptionGroup(
        "cleanup failed",
        [
            ordinary_error,
            BaseExceptionGroup("nested cleanup failed", [timeout_error]),
        ],
    )

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)),
        caplog.at_level(logging.ERROR, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is not cleanup_group
    rendered = logging.Formatter().format(record)
    assert "An additional error occurred during the MCP request." in rendered
    assert "ordinary sibling failure" not in rendered
    _assert_not_retained_in_log_record(record, cleanup_group)
    _assert_not_retained_in_log_record(record, ordinary_error)
    _assert_not_retained_in_log_record(record, timeout_error)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_normal_cleanup_checks_every_nested_transport_error_before_logging(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    cleanup_group, _, unsafe_error = _mixed_request_error_group(_CREDENTIALED_URL)

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)),
        caplog.at_level(logging.WARNING, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.exc_info is None
    assert record.exc_text is None
    _assert_not_retained_in_log_record(record, cleanup_group)
    _assert_not_retained_in_log_record(record, unsafe_error)
    _assert_url_credentials_hidden_from_log_record(record)
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_normal_cleanup_preserves_non_http_exception_group_logging(monkeypatch, caplog):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    cleanup_group = BaseExceptionGroup("cleanup failed", [ValueError("ordinary failure")])

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)),
        caplog.at_level(logging.ERROR, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.exc_info is not None
    assert record.exc_info[1] is cleanup_group
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_normal_cleanup_preserves_cancel_scope_suppression(monkeypatch, caplog):
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()
    cleanup_group = BaseExceptionGroup(
        "cleanup failed",
        [RuntimeError("Attempted to exit cancel scope in a different task")],
    )

    with (
        patch.object(server.exit_stack, "aclose", AsyncMock(side_effect=cleanup_group)),
        caplog.at_level(logging.DEBUG, logger="openai.agents"),
    ):
        await server.cleanup()

    record = caplog.records[-1]
    assert record.levelno == logging.DEBUG
    assert record.exc_info is not None
    assert record.exc_info[1] is cleanup_group
    assert server.session is None
    assert server._get_session_id is None


@pytest.mark.asyncio
async def test_cleanup_propagates_cancellation_and_clears_session_state():
    server = MCPServerSse(params={"url": _SAFE_URL})
    server.session = MagicMock()

    with patch.object(
        server.exit_stack,
        "aclose",
        AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await server.cleanup()

    assert server.session is None
    assert server._get_session_id is None
