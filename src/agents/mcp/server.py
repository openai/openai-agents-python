from __future__ import annotations

import abc
import asyncio
import inspect
import json
import math
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeVar, Union, cast

import anyio
import httpx

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup  # pyright: ignore[reportMissingImports]
from anyio import ClosedResourceError
from mcp import ClientSession, StdioServerParameters, Tool as MCPTool, stdio_client
from mcp.client.session import MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    InitializeResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceResult,
)
from typing_extensions import NotRequired, TypedDict

from .. import _debug
from ..exceptions import UserError
from ..logger import (
    log_tool_action_debug,
    log_tool_action_error,
    log_tool_action_warning,
    logger,
)
from ..run_context import RunContextWrapper
from ..tool import ToolErrorFunction
from ..util._types import MaybeAwaitable
from ._compat import (
    HTTP_CONNECT_ERROR_TYPES,
    HTTP_ERROR_TYPES,
    HTTP_INVALID_URL_TYPES,
    HTTP_REQUEST_ERROR_TYPES,
    HTTP_STATUS_ERROR_TYPES,
    HTTP_TIMEOUT_ERROR_TYPES,
    MCP_HTTPX,
    MCP_V2,
    MCPError,
    clear_result_next_cursor,
    create_v2_client,
    http_reason_phrase,
    http_status_code,
    is_http_connect_error,
    is_http_request_error,
    is_http_status_error,
    is_http_timeout_error,
    is_mcp_connection_closed_error,
    is_mcp_timeout_error,
    resource_uri,
    result_next_cursor,
    streamable_http_client_v2,
    tool_input_schema,
)
from ._logging import get_mcp_server_log_message, get_mcp_server_log_name
from .util import (
    HttpClientFactory,
    MCPToolCustomDataExtractor,
    MCPToolMetaResolver,
    ToolFilter,
    ToolFilterContext,
    ToolFilterStatic,
)


class RequireApprovalToolList(TypedDict, total=False):
    tool_names: list[str]


class RequireApprovalObject(TypedDict, total=False):
    always: RequireApprovalToolList
    never: RequireApprovalToolList


RequireApprovalPolicy = Literal["always", "never"]
RequireApprovalMapping = dict[str, RequireApprovalPolicy]
if TYPE_CHECKING:
    LocalMCPApprovalCallable = Callable[
        [RunContextWrapper[Any], "AgentBase", MCPTool],
        MaybeAwaitable[bool],
    ]
else:
    LocalMCPApprovalCallable = Callable[..., Any]

if TYPE_CHECKING:
    RequireApprovalSetting = (
        RequireApprovalPolicy
        | RequireApprovalObject
        | RequireApprovalMapping
        | LocalMCPApprovalCallable
        | bool
        | None
    )
else:
    RequireApprovalSetting = Union[  # noqa: UP007
        RequireApprovalPolicy,
        RequireApprovalObject,
        RequireApprovalMapping,
        LocalMCPApprovalCallable,
        bool,
        None,
    ]


T = TypeVar("T")
GetSessionIdCallback = Callable[[], str | None]

_streamable_http_module = __import__(
    "mcp.client.streamable_http", fromlist=["StreamableHTTPTransport"]
)
StreamableHTTPTransport = cast(Any, vars(_streamable_http_module)["StreamableHTTPTransport"])
streamablehttp_client = vars(_streamable_http_module).get("streamablehttp_client")

_SAFE_EXCEPTION_GROUP_MESSAGE = "MCP request failed with additional errors."
_SAFE_EXCEPTION_MESSAGE = "An additional error occurred during the MCP request."


def _client_session_read_timeout(timeout_seconds: float | None) -> timedelta | float | None:
    """Convert an MCP read timeout while intentionally treating zero as no timeout."""
    if timeout_seconds is None:
        return None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise TypeError("client_session_timeout_seconds must be a number of seconds or None.")
    if timeout_seconds == 0:
        return None
    try:
        is_finite = math.isfinite(timeout_seconds)
    except OverflowError as error:
        raise ValueError(
            "client_session_timeout_seconds must fit in a datetime.timedelta."
        ) from error
    if not is_finite or timeout_seconds < 0:
        raise ValueError("client_session_timeout_seconds must be zero or a positive finite value.")
    if timeout_seconds < timedelta.resolution.total_seconds():
        raise ValueError("client_session_timeout_seconds must be zero or at least one microsecond.")
    try:
        timeout = timedelta(seconds=timeout_seconds)
    except OverflowError as error:
        raise ValueError(
            "client_session_timeout_seconds must fit in a datetime.timedelta."
        ) from error
    return timeout_seconds if MCP_V2 else timeout


def _transport_error_urls_are_safe(
    http_error: Exception,
) -> bool:
    """Return whether one HTTPX exception contains only credential-safe URLs."""
    request_urls: list[str] = []
    try:
        request_urls.append(str(cast(Any, http_error).request.url))
    except RuntimeError:
        pass

    if is_http_status_error(http_error):
        original_response = cast(Any, http_error).response
        for response in [*original_response.history, original_response]:
            try:
                response_url = response.request.url
            except RuntimeError:
                return False

            request_urls.append(str(response_url))
            redirect_location = response.headers.get("location")
            if redirect_location is not None:
                try:
                    request_urls.append(str(response_url.join(redirect_location)))
                except HTTP_INVALID_URL_TYPES + (ValueError,):
                    return False

    return all(get_mcp_server_log_name(url) == url for url in request_urls)


def _safe_transport_cause(http_error: Exception) -> Exception | None:
    """Keep an unchained transport exception only when its HTTPX URLs are credential-safe."""
    if not _is_http_transport_error(http_error):
        return http_error

    if not _transport_error_urls_are_safe(http_error):
        return None
    if BaseException.__getattribute__(http_error, "__cause__") is not None:
        return None
    if BaseException.__getattribute__(http_error, "__context__") is not None:
        return None
    if BaseException.__getattribute__(http_error, "__dict__").get("__notes__"):
        return None

    return http_error


def _first_unsafe_transport_error(http_errors: list[Exception]) -> Exception | None:
    """Return the first transport error whose HTTPX URLs require sanitization."""
    return next(
        (
            error
            for error in http_errors
            if _is_http_transport_error(error) and not _transport_error_urls_are_safe(error)
        ),
        None,
    )


def _first_unretainable_transport_error(http_errors: list[Exception]) -> Exception | None:
    """Return the first transport error that cannot be retained as an exception cause."""
    return next((error for error in http_errors if _safe_transport_cause(error) is None), None)


def _is_http_transport_error(error: BaseException) -> bool:
    """Return whether an exception is an HTTPX transport error."""
    return is_http_status_error(error) or is_http_request_error(error)


def _credential_safe_exception_group(error_group: BaseExceptionGroup) -> BaseExceptionGroup:
    """Replace an exception group with a fixed-data graph that retains control semantics."""
    safe_exceptions = [
        _credential_safe_exception_group(error)
        if isinstance(error, BaseExceptionGroup)
        else _credential_safe_exception_leaf(error)
        for error in error_group.exceptions
    ]
    return BaseExceptionGroup(_SAFE_EXCEPTION_GROUP_MESSAGE, safe_exceptions)


def _credential_safe_exception_leaf(error: BaseException) -> BaseException:
    """Create a fixed-data replacement for one retained exception leaf."""
    if isinstance(error, asyncio.CancelledError):
        return asyncio.CancelledError()
    if isinstance(error, KeyboardInterrupt):
        return KeyboardInterrupt()
    if isinstance(error, SystemExit):
        return SystemExit()
    if isinstance(error, GeneratorExit):
        return GeneratorExit()
    if isinstance(error, Exception):
        return RuntimeError(_SAFE_EXCEPTION_MESSAGE)
    return BaseException(_SAFE_EXCEPTION_MESSAGE)


def _log_transport_warning(message: str, http_error: Exception) -> None:
    """Log a transport failure without attaching credential-bearing request URLs."""
    if _debug.DONT_LOG_TOOL_DATA:
        log_tool_action_warning(logger, message, http_error)
        return

    safe_error = _safe_transport_cause(http_error)
    if safe_error is None:
        logger.warning("%s", message, stacklevel=3)
        return

    log_tool_action_warning(logger, message, safe_error)


def _get_cleanup_transport_error_message(http_error: Exception) -> str:
    """Return the cleanup warning message for an HTTPX transport failure."""
    if is_http_status_error(http_error):
        return "HTTP error during cleanup of MCP server"
    if is_http_connect_error(http_error):
        return "Connection error during cleanup of MCP server"
    if is_http_timeout_error(http_error):
        return "Timeout error during cleanup of MCP server"
    return "Request error during cleanup of MCP server"


def _log_cleanup_transport_warning(message: str) -> None:
    """Log a fixed cleanup warning without retaining the transport exception."""
    logger.warning("%s", message, stacklevel=3)


def _create_default_streamable_http_client(
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    auth: Any = None,
) -> Any:
    kwargs: dict[str, Any] = {"follow_redirects": False}
    if MCP_V2:
        _validate_v2_http_auth(auth)
        if timeout is not None:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return MCP_HTTPX.AsyncClient(**kwargs)

    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def _validate_v2_http_auth(auth: Any) -> None:
    if auth is None or isinstance(auth, MCP_HTTPX.Auth):
        return
    raise UserError(
        "MCP Python SDK v2 requires auth to be an httpx2.Auth instance. "
        "Use httpx2 authentication, configure an Authorization header, or pin mcp<2."
    )


def _validated_v2_http_client_factory(factory: Callable[..., Any]) -> Callable[..., Any]:
    def create_client(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> Any:
        _validate_v2_http_auth(auth)
        client = factory(headers=headers, timeout=timeout, auth=auth)
        if not isinstance(client, MCP_HTTPX.AsyncClient):
            raise UserError(
                "MCP Python SDK v2 requires httpx_client_factory to return an "
                "httpx2.AsyncClient. Use an httpx2 factory or pin mcp<2."
            )
        return client

    return create_client


def _jsonrpc_request_method(request: Any) -> str | None:
    try:
        payload = json.loads(request.content)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    return method if isinstance(method, str) else None


def _configure_v2_session_id_hook(
    client: Any,
    *,
    on_session_id: Callable[[str], None] | None,
) -> None:
    async def handle_response(response: Any) -> None:
        if response.status_code >= 500:
            response.raise_for_status()
        method = _jsonrpc_request_method(response.request)
        if (
            on_session_id is not None
            and method == "initialize"
            and 200 <= response.status_code < 300
        ):
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                on_session_id(session_id)

    client.event_hooks.setdefault("response", []).append(handle_response)


@asynccontextmanager
async def _streamablehttp_client_v2(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float | timedelta,  # noqa: ASYNC109
    sse_read_timeout: float | timedelta,
    terminate_on_close: bool,
    httpx_client_factory: Callable[..., Any],
    auth: Any,
    on_session_id: Callable[[str], None] | None,
) -> AsyncGenerator[MCPStreamTransport, None]:
    timeout_seconds = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
    sse_read_timeout_seconds = (
        sse_read_timeout.total_seconds()
        if isinstance(sse_read_timeout, timedelta)
        else sse_read_timeout
    )
    factory = _validated_v2_http_client_factory(httpx_client_factory)
    client = factory(
        headers=headers,
        timeout=MCP_HTTPX.Timeout(timeout_seconds, read=sse_read_timeout_seconds),
        auth=auth,
    )
    _configure_v2_session_id_hook(
        client,
        on_session_id=on_session_id,
    )
    async with client:
        async with streamable_http_client_v2(
            url,
            http_client=client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams


class _InitializedNotificationTolerantStreamableHTTPTransport(
    StreamableHTTPTransport  # type: ignore[misc, valid-type]
):
    async def _handle_post_request(self, ctx: Any) -> None:
        message = ctx.session_message.message
        if not self._is_initialized_notification(message):
            await super()._handle_post_request(ctx)
            return

        try:
            await super()._handle_post_request(ctx)
        except HTTP_ERROR_TYPES as exc:
            _log_transport_warning(
                "Ignoring initialized notification HTTP failure",
                exc,
            )
            return


@asynccontextmanager
async def _streamablehttp_client_with_transport(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    # This configures the HTTP client rather than an async cancellation scope.
    timeout: float | timedelta = 30,  # noqa: ASYNC109
    sse_read_timeout: float | timedelta = 60 * 5,
    terminate_on_close: bool = True,
    httpx_client_factory: HttpClientFactory = _create_default_streamable_http_client,
    auth: httpx.Auth | None = None,
    transport_factory: Callable[[str], Any] = StreamableHTTPTransport,
) -> AsyncGenerator[MCPStreamTransport, None]:
    timeout_seconds = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
    sse_read_timeout_seconds = (
        sse_read_timeout.total_seconds()
        if isinstance(sse_read_timeout, timedelta)
        else sse_read_timeout
    )

    client = httpx_client_factory(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds, read=sse_read_timeout_seconds),
        auth=auth,
    )
    transport = transport_factory(url)
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        0
    )
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async with client:
        async with anyio.create_task_group() as tg:
            try:
                if _debug.DONT_LOG_TOOL_DATA:
                    logger.debug("Connecting to StreamableHTTP endpoint")
                else:
                    logger.debug(
                        "Connecting to StreamableHTTP endpoint: %s",
                        get_mcp_server_log_name(url),
                    )

                def start_get_stream() -> None:
                    tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

                tg.start_soon(
                    transport.post_writer,
                    client,
                    write_stream_reader,
                    read_stream_writer,
                    write_stream,
                    start_get_stream,
                    tg,
                )

                try:
                    yield (
                        read_stream,
                        write_stream,
                        transport.get_session_id,
                    )
                finally:
                    if transport.session_id and terminate_on_close:
                        await transport.terminate_session(client)
                    tg.cancel_scope.cancel()
            finally:
                await read_stream_writer.aclose()
                await write_stream.aclose()


def _require_streamablehttp_client_v1() -> Callable[..., Any]:
    if streamablehttp_client is None:  # pragma: no cover - guarded by MCP major
        raise RuntimeError("The legacy streamable HTTP client requires MCP Python SDK v1.")
    return cast(Callable[..., Any], streamablehttp_client)


class _SharedSessionRequestNeedsIsolation(Exception):
    """Raised when a shared-session request should be retried on an isolated session."""


class _IsolatedSessionRetryFailed(Exception):
    """Raised when an isolated-session retry fails after consuming retry budget."""


class _UnsetType:
    pass


_UNSET = _UnsetType()

if TYPE_CHECKING:
    from ..agent import AgentBase


MCPStreamTransport = tuple[Any, Any] | tuple[Any, Any, GetSessionIdCallback | None]


class MCPServer(abc.ABC):
    """Base class for Model Context Protocol servers."""

    def __init__(
        self,
        use_structured_content: bool = False,
        require_approval: RequireApprovalSetting = None,
        failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
        tool_meta_resolver: MCPToolMetaResolver | None = None,
        custom_data_extractor: MCPToolCustomDataExtractor | None = None,
    ):
        """
        Args:
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
            require_approval: Approval policy for tools on this server. Accepts "always"/"never",
                a dict of tool names to those values, a boolean, an object with always/never
                tool lists (mirroring TS requireApproval), or a sync/async callable that receives
                `(run_context, agent, tool)` and returns whether the tool call needs approval.
                Normalized into a needs_approval policy.
            failure_error_function: Optional function used to convert MCP tool failures into
                a model-visible error message. If explicitly set to None, tool errors will be
                raised instead of converted. If left unset, the agent-level configuration (or
                SDK default) will be used.
            tool_meta_resolver: Optional callable that produces MCP request metadata (`_meta`) for
                tool calls. It is invoked by the Agents SDK before calling `call_tool`.
            custom_data_extractor: Optional callable that produces SDK-only custom data for
                emitted MCP tool output items.
        """
        self.use_structured_content = use_structured_content
        self._needs_approval_policy = self._normalize_needs_approval(
            require_approval=require_approval
        )
        self._failure_error_function = failure_error_function
        self.tool_meta_resolver = tool_meta_resolver
        self.custom_data_extractor = custom_data_extractor

    @abc.abstractmethod
    async def connect(self):
        """Connect to the server. For example, this might mean spawning a subprocess or
        opening a network connection. The server is expected to remain connected until
        `cleanup()` is called.
        """
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """A readable name for the server."""
        pass

    @property
    def _error_name(self) -> str:
        """Return a diagnostic server name with URL credentials removed."""
        return get_mcp_server_log_name(self.name)

    @abc.abstractmethod
    async def cleanup(self):
        """Cleanup the server. For example, this might mean closing a subprocess or
        closing a network connection.
        """
        pass

    @abc.abstractmethod
    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        """List the tools available on the server."""
        pass

    @abc.abstractmethod
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Invoke a tool on the server."""
        pass

    @property
    def cached_tools(self) -> list[MCPTool] | None:
        """Return the most recently fetched tools list, if available.

        Implementations may return `None` when tools have not been fetched yet or caching is
        disabled.
        """

        return None

    @abc.abstractmethod
    async def list_prompts(
        self,
    ) -> ListPromptsResult:
        """List the prompts available on the server."""
        pass

    @abc.abstractmethod
    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        """Get a specific prompt from the server."""
        pass

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        """List the resources available on the server.

        Args:
            cursor: An opaque pagination cursor returned in a previous
                :class:`~mcp.types.ListResourcesResult` as ``next_cursor`` under
                MCP v2 or ``nextCursor`` under MCP v1.  Pass it here to fetch the
                next page of results.  ``None`` fetches the first page.

        Returns a :class:`~mcp.types.ListResourcesResult`.  When the result contains
        a ``next_cursor`` field under MCP v2 or ``nextCursor`` under MCP v1, call
        this method again with that cursor to retrieve the next page.  Subclasses
        that do not support resources may leave this unimplemented; it will raise
        :exc:`NotImplementedError` at call time.
        """
        raise NotImplementedError(
            f"MCP server '{self._error_name}' does not support list_resources. "
            "Override this method in your server implementation."
        )

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        """List the resource templates available on the server.

        Args:
            cursor: An opaque pagination cursor returned in a previous
                :class:`~mcp.types.ListResourceTemplatesResult` as ``next_cursor``
                under MCP v2 or ``nextCursor`` under MCP v1.  Pass it here to fetch
                the next page of results.  ``None`` fetches the first page.

        Returns a :class:`~mcp.types.ListResourceTemplatesResult`.  When the result
        contains a ``next_cursor`` field under MCP v2 or ``nextCursor`` under MCP
        v1, call this method again with that cursor to retrieve the next page.
        Subclasses that do not support resource templates may leave this
        unimplemented; it will raise :exc:`NotImplementedError` at call time.
        """
        raise NotImplementedError(
            f"MCP server '{self._error_name}' does not support list_resource_templates. "
            "Override this method in your server implementation."
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read the contents of a specific resource by URI.

        Args:
            uri: The URI of the resource to read. See :class:`~pydantic.networks.AnyUrl`
                for the supported URI formats.

        Returns a :class:`~mcp.types.ReadResourceResult`.  Subclasses that do not
        support resources may leave this unimplemented; it will raise
        :exc:`NotImplementedError` at call time.
        """
        raise NotImplementedError(
            f"MCP server '{self._error_name}' does not support read_resource. "
            "Override this method in your server implementation."
        )

    @staticmethod
    def _normalize_needs_approval(
        *,
        require_approval: RequireApprovalSetting,
    ) -> (
        bool
        | dict[str, bool]
        | Callable[[RunContextWrapper[Any], AgentBase, MCPTool], MaybeAwaitable[bool]]
    ):
        """Normalize approval inputs to booleans or a name->bool map."""

        if require_approval is None:
            return False

        def _to_bool(value: object, *, location: str) -> bool:
            if value == "always":
                return True
            if value == "never":
                return False
            raise UserError(
                f"Invalid require_approval value at {location}: "
                f"expected 'always' or 'never', got {value!r}."
            )

        def _validate_tool_names(value: object, *, location: str) -> list[str]:
            if not isinstance(value, list):
                raise UserError(
                    f"Invalid require_approval tool_names at {location}: "
                    f"expected a list of strings, got {type(value).__name__}."
                )

            tool_names: list[str] = []
            for index, tool_name in enumerate(value):
                if not isinstance(tool_name, str):
                    raise UserError(
                        f"Invalid require_approval tool name at {location}[{index}]: "
                        f"expected a string, got {type(tool_name).__name__}."
                    )
                tool_names.append(tool_name)
            return tool_names

        def _get_tool_names_entry(value: object, *, policy: str) -> list[str]:
            if not isinstance(value, dict):
                raise UserError(
                    f"Invalid require_approval.{policy}: "
                    f"expected an object with tool_names, got {type(value).__name__}."
                )
            return _validate_tool_names(
                value.get("tool_names", []),
                location=f"require_approval.{policy}.tool_names",
            )

        def _is_tool_list_schema(value: object) -> bool:
            if not isinstance(value, dict):
                return False
            for key in ("always", "never"):
                if key not in value:
                    continue
                entry = value.get(key)
                if isinstance(entry, dict) and "tool_names" in entry:
                    return True
            return False

        if isinstance(require_approval, dict) and _is_tool_list_schema(require_approval):
            always_entry: RequireApprovalToolList | Any = require_approval.get("always", {})
            never_entry: RequireApprovalToolList | Any = require_approval.get("never", {})
            invalid_keys = sorted(set(require_approval) - {"always", "never"})
            if invalid_keys:
                raise UserError(
                    "Invalid require_approval tool list policy: "
                    f"unexpected keys {invalid_keys!r}; expected only 'always' and 'never'."
                )
            always_names = _get_tool_names_entry(always_entry, policy="always")
            never_names = _get_tool_names_entry(never_entry, policy="never")
            overlapping_names = sorted(set(always_names) & set(never_names))
            if overlapping_names:
                raise UserError(
                    "Invalid require_approval tool list policy: "
                    f"tool names cannot appear in both always and never: {overlapping_names!r}."
                )
            tool_list_mapping: dict[str, bool] = {}
            for name in always_names:
                tool_list_mapping[name] = True
            for name in never_names:
                tool_list_mapping[name] = False
            return tool_list_mapping

        if isinstance(require_approval, dict):
            tool_mapping: dict[str, bool] = {}
            for name, value in require_approval.items():
                if isinstance(value, bool):
                    tool_mapping[str(name)] = value
                else:
                    tool_mapping[str(name)] = _to_bool(
                        value, location=f"require_approval[{name!r}]"
                    )
            return tool_mapping

        if callable(require_approval):
            return require_approval

        if isinstance(require_approval, bool):
            return require_approval

        return _to_bool(require_approval, location="require_approval")

    def _get_needs_approval_for_tool(
        self,
        tool: MCPTool,
        agent: AgentBase | None,
    ) -> bool | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]:
        """Return a FunctionTool.needs_approval value for a given MCP tool.

        Legacy callers may omit ``agent`` when using ``MCPUtil.to_function_tool()`` directly.
        When approval is configured with a callable policy and no agent is available, this method
        returns ``True`` to preserve the historical fail-closed behavior.
        """

        policy = self._needs_approval_policy

        if callable(policy):
            if agent is None:
                return True

            async def _needs_approval(
                run_context: RunContextWrapper[Any], _args: dict[str, Any], _call_id: str
            ) -> bool:
                result = policy(run_context, agent, tool)
                if inspect.isawaitable(result):
                    result = await result
                return bool(result)

            return _needs_approval

        if isinstance(policy, dict):
            return bool(policy.get(tool.name, False))

        return bool(policy)

    def _get_failure_error_function(
        self, agent_failure_error_function: ToolErrorFunction | None
    ) -> ToolErrorFunction | None:
        """Return the effective error handler for MCP tool failures."""
        if self._failure_error_function is _UNSET:
            return agent_failure_error_function
        return cast(ToolErrorFunction | None, self._failure_error_function)


class _MCPServerWithClientSession(MCPServer, abc.ABC):
    """Base class for MCP servers that use a `ClientSession` to communicate with the server."""

    @property
    def cached_tools(self) -> list[MCPTool] | None:
        return self._tools_list

    def __init__(
        self,
        cache_tools_list: bool,
        client_session_timeout_seconds: float | None,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
        max_retry_attempts: int = 0,
        retry_backoff_seconds_base: float = 1.0,
        message_handler: MessageHandlerFnT | None = None,
        require_approval: RequireApprovalSetting = None,
        failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
        tool_meta_resolver: MCPToolMetaResolver | None = None,
        custom_data_extractor: MCPToolCustomDataExtractor | None = None,
    ):
        """
        Args:
            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
            cached and only fetched from the server once. If `False`, the tools list will be
            fetched from the server on each call to `list_tools()`. The cache can be invalidated
            by calling `invalidate_tools_cache()`. You should set this to `True` if you know the
            server will not change its tools list, because it can drastically improve latency
            (by avoiding a round-trip to the server every time).

            client_session_timeout_seconds: The MCP ClientSession read timeout. Positive finite
                values representable by `datetime.timedelta` and at least one microsecond set a
                timeout; `None` and `0` disable it. Other values are rejected during server
                construction.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
            max_retry_attempts: Number of times to retry failed list_tools/call_tool calls.
                Defaults to no retries.
            retry_backoff_seconds_base: The base delay, in seconds, used for exponential
                backoff between retries.
            message_handler: Optional handler invoked for session messages as delivered by the
                ClientSession.
            require_approval: Approval policy for tools on this server. Accepts "always"/"never",
                a dict of tool names to those values, a boolean, or an object with always/never
                tool lists.
            failure_error_function: Optional function used to convert MCP tool failures into
                a model-visible error message. If explicitly set to None, tool errors will be
                raised instead of converted. If left unset, the agent-level configuration (or
                SDK default) will be used.
            tool_meta_resolver: Optional callable that produces MCP request metadata (`_meta`) for
                tool calls. It is invoked by the Agents SDK before calling `call_tool`.
            custom_data_extractor: Optional callable that produces SDK-only custom data for
                emitted MCP tool output items.
        """
        super().__init__(
            use_structured_content=use_structured_content,
            require_approval=require_approval,
            failure_error_function=failure_error_function,
            tool_meta_resolver=tool_meta_resolver,
            custom_data_extractor=custom_data_extractor,
        )
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self._request_lock: asyncio.Lock = asyncio.Lock()
        self.cache_tools_list = cache_tools_list
        self.server_initialize_result: InitializeResult | None = None

        # Validate during construction, then convert again when connecting in case callers mutate
        # the public timeout attribute before a later connection attempt.
        _client_session_read_timeout(client_session_timeout_seconds)
        self.client_session_timeout_seconds = client_session_timeout_seconds
        self.max_retry_attempts = max_retry_attempts
        self.retry_backoff_seconds_base = retry_backoff_seconds_base
        self.message_handler = message_handler

        # The cache is always dirty at startup, so that we fetch tools at least once
        self._cache_dirty = True
        self._tools_list: list[MCPTool] | None = None

        self.tool_filter = tool_filter
        self._serialize_session_requests = False
        self._get_session_id: GetSessionIdCallback | None = None
        self._v2_session_id: str | None = None

    async def _maybe_serialize_request(self, func: Callable[[], Awaitable[T]]) -> T:
        if not self._serialize_session_requests:
            return await func()
        async with self._request_lock:
            return await func()

    async def _list_tools_page(
        self, session: ClientSession, cursor: str | None = None
    ) -> ListToolsResult:
        return await self._maybe_serialize_request(
            lambda: session.list_tools()
            if cursor is None
            else session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        )

    async def _list_prompts_page(
        self, session: ClientSession, cursor: str | None = None
    ) -> ListPromptsResult:
        return await self._run_request_with_transport_error_redaction(
            "list prompts",
            lambda: self._maybe_serialize_request(
                lambda: session.list_prompts()
                if cursor is None
                else session.list_prompts(params=PaginatedRequestParams(cursor=cursor))
            ),
        )

    async def _apply_tool_filter(
        self,
        tools: list[MCPTool],
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        """Apply the tool filter to the list of tools."""
        if self.tool_filter is None:
            return tools

        # Handle static tool filter
        if isinstance(self.tool_filter, dict):
            return self._apply_static_tool_filter(tools, self.tool_filter)

        # Handle callable tool filter (dynamic filter)
        else:
            if run_context is None or agent is None:
                raise UserError("run_context and agent are required for dynamic tool filtering")
            return await self._apply_dynamic_tool_filter(tools, run_context, agent)

    def _apply_static_tool_filter(
        self, tools: list[MCPTool], static_filter: ToolFilterStatic
    ) -> list[MCPTool]:
        """Apply static tool filtering based on allowlist and blocklist."""
        filtered_tools = tools

        # Apply allowed_tool_names filter (whitelist)
        if "allowed_tool_names" in static_filter:
            allowed_names = static_filter["allowed_tool_names"]
            filtered_tools = [t for t in filtered_tools if t.name in allowed_names]

        # Apply blocked_tool_names filter (blacklist)
        if "blocked_tool_names" in static_filter:
            blocked_names = static_filter["blocked_tool_names"]
            filtered_tools = [t for t in filtered_tools if t.name not in blocked_names]

        return filtered_tools

    async def _apply_dynamic_tool_filter(
        self,
        tools: list[MCPTool],
        run_context: RunContextWrapper[Any],
        agent: AgentBase,
    ) -> list[MCPTool]:
        """Apply dynamic tool filtering using a callable filter function."""

        # Ensure we have a callable filter
        if not callable(self.tool_filter):
            raise ValueError("Tool filter must be callable for dynamic filtering")
        tool_filter_func = self.tool_filter

        # Create filter context
        filter_context = ToolFilterContext(
            run_context=run_context,
            agent=agent,
            server_name=self.name,
        )

        filtered_tools = []
        for tool in tools:
            try:
                # Call the filter function with context
                result = tool_filter_func(filter_context, tool)

                if inspect.isawaitable(result):
                    should_include = await result
                else:
                    should_include = result

                if should_include:
                    filtered_tools.append(tool)
            except Exception as e:
                if _debug.DONT_LOG_TOOL_DATA:
                    message = "Error applying MCP tool filter"
                else:
                    server_name = get_mcp_server_log_name(self.name)
                    message = (
                        f"Error applying MCP tool filter to tool '{tool.name}' "
                        f"on server '{server_name}'"
                    )
                log_tool_action_error(logger, message, e)
                # On error, exclude the tool for safety
                continue

        return filtered_tools

    @abc.abstractmethod
    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[MCPStreamTransport]:
        """Create the streams for the server."""
        pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    def invalidate_tools_cache(self):
        """Invalidate the tools cache."""
        self._cache_dirty = True

    def _extract_http_errors_from_exception(self, e: BaseException) -> list[Exception]:
        """Extract all HTTP errors from an exception or nested ExceptionGroup."""
        if _is_http_transport_error(e):
            assert isinstance(e, Exception)
            return [e]

        if isinstance(e, BaseExceptionGroup):
            http_errors: list[Exception] = []
            for exc in e.exceptions:
                http_errors.extend(self._extract_http_errors_from_exception(exc))
            return http_errors

        return []

    def _select_cleanup_transport_error(self, error: BaseException) -> Exception | None:
        """Select a cleanup transport error for specialized handling."""
        unsafe_http_error = _first_unsafe_transport_error(
            self._extract_http_errors_from_exception(error)
        )
        if unsafe_http_error is not None:
            return unsafe_http_error

        candidates = error.exceptions if isinstance(error, BaseExceptionGroup) else (error,)
        for error_types in (
            HTTP_STATUS_ERROR_TYPES,
            HTTP_CONNECT_ERROR_TYPES,
            HTTP_TIMEOUT_ERROR_TYPES,
        ):
            selected_http_error = next(
                (
                    candidate
                    for candidate in reversed(candidates)
                    if isinstance(candidate, Exception) and isinstance(candidate, error_types)
                ),
                None,
            )
            if selected_http_error is not None:
                return selected_http_error

        return None

    def _user_error_for_http_error(
        self,
        http_error: Exception,
        *,
        include_http_reason_phrase: bool = True,
    ) -> UserError:
        """Build a UserError from safe HTTP diagnostics."""
        error_message = f"Failed to connect to MCP server '{self._error_name}': "
        if is_http_status_error(http_error):
            error_message += f"HTTP error {http_status_code(http_error)}"
            if include_http_reason_phrase:
                error_message += f" ({http_reason_phrase(http_error)})"

        elif is_http_connect_error(http_error):
            error_message += "Could not reach the server."

        elif is_http_timeout_error(http_error):
            error_message += "Connection timeout."

        elif is_http_request_error(http_error):
            error_message += "Request failed."

        return UserError(error_message)

    @staticmethod
    def _raise_mapped_transport_error(error: UserError, cause: Exception | None) -> NoReturn:
        """Raise a mapped transport error without retaining unsafe URL data."""
        if cause is None:
            raise error from None
        raise error from cause

    def _user_error_for_request_operation(
        self,
        operation: str,
        http_error: Exception,
    ) -> UserError:
        """Build a credential-safe error for an MCP request operation."""
        error_message = f"Failed to {operation} on MCP server '{self._error_name}': "
        if is_http_status_error(http_error):
            error_message += f"HTTP error {http_status_code(http_error)}"
        elif is_http_connect_error(http_error):
            error_message += "Connection lost. The server may have disconnected."
        elif is_http_timeout_error(http_error):
            error_message += "Connection timeout."
        else:
            error_message += "Request failed."
        return UserError(error_message)

    async def _run_request_with_transport_error_redaction(
        self,
        operation: str,
        func: Callable[[], Awaitable[T]],
    ) -> T:
        """Run an MCP request without retaining credential-bearing HTTP errors."""
        transport_error: UserError | None = None
        base_error_group: BaseExceptionGroup | None = None
        try:
            return await func()
        except HTTP_STATUS_ERROR_TYPES + HTTP_REQUEST_ERROR_TYPES as http_error:
            transport_error = self._user_error_for_request_operation(operation, http_error)
        except BaseExceptionGroup as error_group:
            http_errors = self._extract_http_errors_from_exception(error_group)
            if not http_errors:
                raise
            selected_http_error = http_errors[0]
            http_group, remaining_group = error_group.split(_is_http_transport_error)
            assert http_group is not None
            mapped_transport_error = self._user_error_for_request_operation(
                operation,
                selected_http_error,
            )
            if remaining_group is None:
                transport_error = mapped_transport_error
            else:
                safe_remaining_group = _credential_safe_exception_group(remaining_group)
                base_error_group = BaseExceptionGroup(
                    _SAFE_EXCEPTION_GROUP_MESSAGE,
                    [mapped_transport_error, *safe_remaining_group.exceptions],
                )
            http_errors.clear()
            del selected_http_error
            del http_group
            del remaining_group

        if base_error_group is not None:
            raise base_error_group
        assert transport_error is not None
        self._raise_mapped_transport_error(transport_error, None)

    async def _run_with_retries(self, func: Callable[[], Awaitable[T]]) -> T:
        attempts = 0
        while True:
            try:
                return await func()
            except Exception:
                attempts += 1
                if self.max_retry_attempts != -1 and attempts > self.max_retry_attempts:
                    raise
                backoff = self.retry_backoff_seconds_base * (2 ** (attempts - 1))
                await asyncio.sleep(backoff)

    @asynccontextmanager
    async def _client_session_context(self, read_timeout: timedelta | float | None):
        """Create one initialized or discovered client session for the installed MCP major."""
        async with AsyncExitStack() as exit_stack:
            if MCP_V2:
                v2_timeout = cast(float | None, read_timeout)
                session_ready: asyncio.Future[ClientSession] = (
                    asyncio.get_running_loop().create_future()
                )
                close_client = asyncio.Event()

                async def run_client() -> None:
                    try:
                        client = create_v2_client(
                            self.create_streams(),
                            read_timeout_seconds=v2_timeout,
                            message_handler=self.message_handler,
                        )
                        async with client as connected_client:
                            session_ready.set_result(connected_client.session)
                            await close_client.wait()
                    except BaseException as exc:
                        if not session_ready.done():
                            session_ready.set_exception(exc)
                            return
                        raise

                client_task = asyncio.create_task(run_client())
                try:
                    try:
                        session = await asyncio.shield(session_ready)
                    except asyncio.CancelledError:
                        session_ready.cancel()
                        client_task.cancel()
                        try:
                            await client_task
                        except BaseException:
                            pass
                        raise
                    except BaseException:
                        await client_task
                        raise

                    try:
                        yield session
                    finally:
                        close_client.set()
                        try:
                            await client_task
                        except asyncio.CancelledError:
                            client_task.cancel()
                            try:
                                await client_task
                            except BaseException:
                                pass
                            raise
                finally:
                    close_client.set()
                return

            transport = await exit_stack.enter_async_context(self.create_streams())
            read, write, *rest = transport
            session = await exit_stack.enter_async_context(
                cast(Any, ClientSession)(
                    read,
                    write,
                    cast(timedelta | None, read_timeout),
                    message_handler=self.message_handler,
                )
            )
            await session.initialize()
            yield session

    async def connect(self):
        """Connect to the server."""
        read_timeout = _client_session_read_timeout(self.client_session_timeout_seconds)
        connection_succeeded = False
        connection_error: UserError | None = None
        connection_cause: Exception | None = None
        connection_exception: BaseException | None = None
        cleanup_failure: BaseException | None = None
        try:
            if MCP_V2:
                session = await self.exit_stack.enter_async_context(
                    self._client_session_context(read_timeout)
                )
                self.server_initialize_result = getattr(session, "initialize_result", None)
            else:
                v1_read_timeout = cast(timedelta | None, read_timeout)
                transport = await self.exit_stack.enter_async_context(self.create_streams())
                read, write, *rest = transport
                self._get_session_id = rest[0] if rest and callable(rest[0]) else None
                session = await self.exit_stack.enter_async_context(
                    cast(Any, ClientSession)(
                        read,
                        write,
                        v1_read_timeout,
                        message_handler=self.message_handler,
                    )
                )
                self.server_initialize_result = await session.initialize()
            self.session = session
            connection_succeeded = True
        except BaseException as e:
            if not isinstance(e, Exception):
                connection_exception = e
            else:
                http_errors = self._extract_http_errors_from_exception(e)
                if not http_errors:
                    connection_exception = e
                else:
                    unsafe_http_error = _first_unretainable_transport_error(http_errors)
                    http_error = unsafe_http_error or http_errors[0]
                    connection_cause = _safe_transport_cause(http_error)
                    maps_safe_error = (
                        is_http_status_error(http_error)
                        or is_http_connect_error(http_error)
                        or is_http_timeout_error(http_error)
                    )
                    if connection_cause is not None and not maps_safe_error:
                        connection_exception = e
                        connection_cause = None
                    else:
                        connection_error = self._user_error_for_http_error(http_error)
                    http_errors.clear()
                    del http_error
                    del unsafe_http_error

        # Run cleanup after leaving the connection exception handler so a cleanup UserError does
        # not retain the pending connection failure as its implicit context.
        if not connection_succeeded:
            try:
                await self.cleanup()
            except UserError as e:
                cleanup_failure = e
            except Exception as cleanup_error:
                # Suppress RuntimeError about cancel scopes during cleanup - this is a known
                # issue with the MCP library's async generator cleanup and shouldn't mask the
                # original error.
                if isinstance(cleanup_error, RuntimeError) and "cancel scope" in str(cleanup_error):
                    logger.debug(
                        "%s",
                        get_mcp_server_log_message(
                            "Ignoring cancel scope error during cleanup of MCP server", self
                        ),
                        stacklevel=2,
                    )
                else:
                    # Log other cleanup errors but don't raise - original error is more important.
                    logger.warning(
                        "%s",
                        get_mcp_server_log_message("Error during cleanup of MCP server", self),
                        stacklevel=2,
                    )
            except BaseException as e:
                cleanup_failure = e

        if cleanup_failure is not None:
            connection_exception = None
            connection_error = None
            connection_cause = None
            if isinstance(cleanup_failure, UserError):
                self._raise_mapped_transport_error(cleanup_failure, None)
            raise cleanup_failure

        if connection_exception is not None:
            raise connection_exception

        if connection_error is not None:
            self._raise_mapped_transport_error(connection_error, connection_cause)

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        """List the tools available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None

        transport_error: UserError | None = None
        transport_cause: Exception | None = None
        try:
            tools: list[MCPTool]
            # Return from cache if caching is enabled, we have tools, and the cache is not dirty
            if self.cache_tools_list and not self._cache_dirty and self._tools_list:
                tools = self._tools_list
            else:
                tools = []
                cursor: str | None = None
                seen_cursors: set[str | None] = set()

                async def fetch_pages() -> bool:
                    nonlocal cursor
                    while True:
                        result = await self._list_tools_page(session, cursor)
                        tools.extend(result.tools)
                        seen_cursors.add(cursor)
                        next_cursor = result_next_cursor(result)
                        if next_cursor is None:
                            return True
                        if next_cursor in seen_cursors:
                            return False
                        cursor = next_cursor

                pagination_complete = False
                pagination_failure: BaseException | None = None
                try:
                    pagination_complete = await self._run_with_retries(fetch_pages)
                except BaseException as error:
                    if cursor is None:
                        raise
                    if isinstance(error, BaseExceptionGroup):
                        pagination_failure = _credential_safe_exception_group(error)
                    elif isinstance(error, Exception):
                        pagination_failure = self._user_error_for_request_operation(
                            "list tools", error
                        )
                    else:
                        pagination_failure = _credential_safe_exception_leaf(error)

                if pagination_failure is not None or not pagination_complete:
                    cursor = None
                    seen_cursors.clear()
                    tools.clear()
                    del fetch_pages
                    if pagination_failure is not None:
                        raise pagination_failure from None
                    raise UserError(
                        f"MCP server '{self._error_name}' returned a repeated cursor while "
                        "listing tools."
                    ) from None

                cursor = None
                seen_cursors.clear()
                del fetch_pages
                self._tools_list = tools
                self._cache_dirty = False

            # Filter tools based on tool_filter
            filtered_tools = tools
            if self.tool_filter is not None:
                filtered_tools = await self._apply_tool_filter(filtered_tools, run_context, agent)
            return filtered_tools
        except HTTP_STATUS_ERROR_TYPES as e:
            status_code = http_status_code(e)
            transport_error = UserError(
                f"Failed to list tools from MCP server '{self._error_name}': "
                f"HTTP error {status_code}"
            )
            transport_cause = _safe_transport_cause(e)
        except HTTP_REQUEST_ERROR_TYPES as e:
            transport_cause = _safe_transport_cause(e)
            if transport_cause is not None and not is_http_connect_error(e):
                raise
            if is_http_connect_error(e):
                transport_error = UserError(
                    f"Failed to list tools from MCP server '{self._error_name}': Connection lost. "
                    f"The server may have disconnected."
                )
            elif is_http_timeout_error(e):
                transport_error = UserError(
                    f"Failed to list tools from MCP server '{self._error_name}': "
                    "Connection timeout."
                )
            else:
                transport_error = UserError(
                    f"Failed to list tools from MCP server '{self._error_name}': Request failed."
                )

        assert transport_error is not None
        self._raise_mapped_transport_error(transport_error, transport_cause)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Invoke a tool on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None

        transport_error: UserError | None = None
        transport_cause: Exception | None = None
        try:
            self._validate_required_parameters(tool_name=tool_name, arguments=arguments)
            if meta is None:
                return await self._run_with_retries(
                    lambda: self._maybe_serialize_request(
                        lambda: session.call_tool(tool_name, arguments)
                    )
                )
            return await self._run_with_retries(
                lambda: self._maybe_serialize_request(
                    lambda: cast(Any, session).call_tool(tool_name, arguments, meta=meta)
                )
            )
        except HTTP_STATUS_ERROR_TYPES as e:
            status_code = http_status_code(e)
            transport_error = UserError(
                f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                f"HTTP error {status_code}"
            )
            transport_cause = _safe_transport_cause(e)
        except HTTP_REQUEST_ERROR_TYPES as e:
            transport_cause = _safe_transport_cause(e)
            if transport_cause is not None and not is_http_connect_error(e):
                raise
            if is_http_connect_error(e):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection lost. The server may have disconnected."
                )
            elif is_http_timeout_error(e):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection timeout."
                )
            else:
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Request failed."
                )

        assert transport_error is not None
        self._raise_mapped_transport_error(transport_error, transport_cause)

    def _validate_required_parameters(
        self, tool_name: str, arguments: dict[str, Any] | None
    ) -> None:
        """Validate required tool parameters from cached MCP tool schemas before invocation."""
        if self._tools_list is None:
            return

        tool = next((item for item in self._tools_list if item.name == tool_name), None)
        if tool is None or not isinstance(tool_input_schema(tool), dict):
            return

        raw_required = tool_input_schema(tool).get("required")
        if not isinstance(raw_required, list) or not raw_required:
            return

        if arguments is None:
            arguments_to_validate: dict[str, Any] = {}
        elif isinstance(arguments, dict):
            arguments_to_validate = arguments
        else:
            raise UserError(
                f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                "arguments must be an object."
            )

        required_names = [name for name in raw_required if isinstance(name, str)]
        missing = [name for name in required_names if name not in arguments_to_validate]
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise UserError(
                f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                f"missing required parameters: {missing_text}"
            )

    async def list_prompts(
        self,
    ) -> ListPromptsResult:
        """List the prompts available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None
        result = await self._list_prompts_page(session)
        if result_next_cursor(result) is None:
            return result

        prompts = list(result.prompts)
        cursor: str | None = result_next_cursor(result)
        seen_cursors: set[str | None] = {None}
        pagination_failure: BaseException | None = None
        repeated_cursor = False
        page: ListPromptsResult | None = None
        next_cursor: str | None = None
        while cursor is not None:
            try:
                page = await self._list_prompts_page(session, cursor)
            except BaseException as error:
                if isinstance(error, BaseExceptionGroup):
                    pagination_failure = _credential_safe_exception_group(error)
                elif isinstance(error, Exception):
                    pagination_failure = self._user_error_for_request_operation(
                        "list prompts", error
                    )
                else:
                    pagination_failure = _credential_safe_exception_leaf(error)
                break
            prompts.extend(page.prompts)
            seen_cursors.add(cursor)
            next_cursor = result_next_cursor(page)
            if next_cursor is not None and next_cursor in seen_cursors:
                repeated_cursor = True
                break
            cursor = next_cursor

        if pagination_failure is not None or repeated_cursor:
            cursor = None
            seen_cursors.clear()
            prompts.clear()
            page = None
            next_cursor = None
            del result
            if pagination_failure is not None:
                raise pagination_failure from None
            raise UserError(
                f"MCP server '{self._error_name}' returned a repeated cursor while listing prompts."
            ) from None

        return cast(ListPromptsResult, clear_result_next_cursor(result, prompts=prompts))

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        """Get a specific prompt from the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None
        return await self._run_request_with_transport_error_redaction(
            "get prompt",
            lambda: self._maybe_serialize_request(lambda: session.get_prompt(name, arguments)),
        )

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        """List the resources available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None
        return await self._run_request_with_transport_error_redaction(
            "list resources",
            lambda: self._maybe_serialize_request(
                lambda: (
                    session.list_resources()
                    if cursor is None
                    else session.list_resources(params=PaginatedRequestParams(cursor=cursor))
                )
                if MCP_V2
                else cast(Any, session).list_resources(cursor)
            ),
        )

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        """List the resource templates available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None
        return await self._run_request_with_transport_error_redaction(
            "list resource templates",
            lambda: self._maybe_serialize_request(
                lambda: (
                    session.list_resource_templates()
                    if cursor is None
                    else session.list_resource_templates(
                        params=PaginatedRequestParams(cursor=cursor)
                    )
                )
                if MCP_V2
                else cast(Any, session).list_resource_templates(cursor)
            ),
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read the contents of a specific resource by URI.

        Args:
            uri: The URI of the resource to read. See :class:`~pydantic.networks.AnyUrl`
                for the supported URI formats.
        """
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")
        session = self.session
        assert session is not None
        return await self._run_request_with_transport_error_redaction(
            "read resource",
            lambda: self._maybe_serialize_request(
                lambda: cast(Any, session).read_resource(resource_uri(uri))
            ),
        )

    async def cleanup(self):
        """Cleanup the server."""
        async with self._cleanup_lock:
            # Only raise HTTP errors if we're cleaning up after a failed connection.
            # During normal teardown (via __aexit__), log but don't raise to avoid
            # masking the original exception.
            is_failed_connection_cleanup = self.session is None
            cleanup_error: UserError | None = None

            try:
                await self.exit_stack.aclose()
            except asyncio.CancelledError as e:
                log_tool_action_debug(
                    logger,
                    get_mcp_server_log_message("Cleanup cancelled for MCP server", self),
                    e,
                )
                raise
            except (  # type: ignore[misc]
                BaseExceptionGroup,
                *HTTP_STATUS_ERROR_TYPES,
                *HTTP_REQUEST_ERROR_TYPES,
            ) as e:
                selected_http_error = self._select_cleanup_transport_error(e)
                if selected_http_error is not None:
                    if is_failed_connection_cleanup:
                        cleanup_error = self._user_error_for_http_error(
                            selected_http_error,
                            include_http_reason_phrase=False,
                        )
                        del selected_http_error
                    else:
                        _log_cleanup_transport_warning(
                            get_mcp_server_log_message(
                                _get_cleanup_transport_error_message(selected_http_error), self
                            )
                        )
                elif is_http_request_error(e):
                    _log_cleanup_transport_warning(
                        get_mcp_server_log_message(_get_cleanup_transport_error_message(e), self)
                    )
                elif isinstance(e, BaseExceptionGroup):
                    http_errors = self._extract_http_errors_from_exception(e)
                    if http_errors:
                        safe_error_group = _credential_safe_exception_group(e)
                        log_tool_action_error(
                            logger,
                            get_mcp_server_log_message("Error cleaning up MCP server", self),
                            safe_error_group,
                        )
                    else:
                        # No HTTP error found, suppress RuntimeError about cancel scopes.
                        has_cancel_scope_error = any(
                            isinstance(exc, RuntimeError) and "cancel scope" in str(exc)
                            for exc in e.exceptions
                        )
                        if has_cancel_scope_error:
                            log_tool_action_debug(
                                logger,
                                get_mcp_server_log_message(
                                    "Ignoring cancel scope error during cleanup of MCP server", self
                                ),
                                e,
                            )
                        else:
                            log_tool_action_error(
                                logger,
                                get_mcp_server_log_message("Error cleaning up MCP server", self),
                                e,
                            )
                else:
                    log_tool_action_error(
                        logger,
                        get_mcp_server_log_message("Error cleaning up MCP server", self),
                        e,
                    )
            except Exception as e:
                # Suppress RuntimeError about cancel scopes - this is a known issue with the MCP
                # library when background tasks fail during async generator cleanup
                if isinstance(e, RuntimeError) and "cancel scope" in str(e):
                    log_tool_action_debug(
                        logger,
                        get_mcp_server_log_message(
                            "Ignoring cancel scope error during cleanup of MCP server", self
                        ),
                        e,
                    )
                else:
                    log_tool_action_error(
                        logger,
                        get_mcp_server_log_message("Error cleaning up MCP server", self),
                        e,
                    )
            finally:
                self.session = None
                self._get_session_id = None
                self._v2_session_id = None

            if cleanup_error is not None:
                self._raise_mapped_transport_error(cleanup_error, None)


class MCPServerStdioParams(TypedDict):
    """Mirrors `mcp.client.stdio.StdioServerParameters`, but lets you pass params without another
    import.
    """

    command: str
    """The executable to run to start the server. For example, `python` or `node`."""

    args: NotRequired[list[str]]
    """Command line args to pass to the `command` executable. For example, `['foo.py']` or
    `['server.js', '--port', '8080']`."""

    env: NotRequired[dict[str, str]]
    """The environment variables to set for the server."""

    cwd: NotRequired[str | Path]
    """The working directory to use when spawning the process."""

    encoding: NotRequired[str]
    """The text encoding used when sending/receiving messages to the server. Defaults to `utf-8`."""

    encoding_error_handler: NotRequired[Literal["strict", "ignore", "replace"]]
    """The text encoding error handler. Defaults to `strict`.

    See https://docs.python.org/3/library/codecs.html#codec-base-classes for
    explanations of possible values.
    """


class MCPServerStdio(_MCPServerWithClientSession):
    """MCP server implementation that uses the stdio transport. See the [spec]
    (https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/transports/#stdio) for
    details.
    """

    def __init__(
        self,
        params: MCPServerStdioParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
        max_retry_attempts: int = 0,
        retry_backoff_seconds_base: float = 1.0,
        message_handler: MessageHandlerFnT | None = None,
        require_approval: RequireApprovalSetting = None,
        failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
        tool_meta_resolver: MCPToolMetaResolver | None = None,
        custom_data_extractor: MCPToolCustomDataExtractor | None = None,
    ):
        """Create a new MCP server based on the stdio transport.

        Args:
            params: The params that configure the server. This includes the command to run to
                start the server, the args to pass to the command, the environment variables to
                set for the server, the working directory to use when spawning the process, and
                the text encoding used when sending/receiving messages to the server.
            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).
            name: A readable name for the server. If not provided, we'll create one from the
                command.
            client_session_timeout_seconds: The MCP ClientSession read timeout. Positive finite
                values representable by `datetime.timedelta` and at least one microsecond set a
                timeout; `None` and `0` disable it. Other values are rejected during server
                construction.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
            max_retry_attempts: Number of times to retry failed list_tools/call_tool calls.
                Defaults to no retries.
            retry_backoff_seconds_base: The base delay, in seconds, for exponential
                backoff between retries.
            message_handler: Optional handler invoked for session messages as delivered by the
                ClientSession.
            require_approval: Approval policy for tools on this server. Accepts "always"/"never",
                a dict of tool names to those values, or an object with always/never tool lists.
            failure_error_function: Optional function used to convert MCP tool failures into
                a model-visible error message. If explicitly set to None, tool errors will be
                raised instead of converted. If left unset, the agent-level configuration (or
                SDK default) will be used.
            tool_meta_resolver: Optional callable that produces MCP request metadata (`_meta`) for
                tool calls. It is invoked by the Agents SDK before calling `call_tool`.
            custom_data_extractor: Optional callable that produces SDK-only custom data for
                emitted MCP tool output items.
        """
        super().__init__(
            cache_tools_list=cache_tools_list,
            client_session_timeout_seconds=client_session_timeout_seconds,
            tool_filter=tool_filter,
            use_structured_content=use_structured_content,
            max_retry_attempts=max_retry_attempts,
            retry_backoff_seconds_base=retry_backoff_seconds_base,
            message_handler=message_handler,
            require_approval=require_approval,
            failure_error_function=failure_error_function,
            tool_meta_resolver=tool_meta_resolver,
            custom_data_extractor=custom_data_extractor,
        )

        self.params = StdioServerParameters(
            command=params["command"],
            args=params.get("args", []),
            env=params.get("env"),
            cwd=params.get("cwd"),
            encoding=params.get("encoding", "utf-8"),
            encoding_error_handler=params.get("encoding_error_handler", "strict"),
        )

        self._name = name or f"stdio: {self.params.command}"

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[MCPStreamTransport]:
        """Create the streams for the server."""
        return stdio_client(self.params)

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name


class MCPServerSseParams(TypedDict):
    """Mirrors the params in `mcp.client.sse.sse_client`."""

    url: str
    """The URL of the server."""

    headers: NotRequired[dict[str, str]]
    """The headers to send to the server."""

    timeout: NotRequired[float]
    """The timeout for the HTTP request. Defaults to 5 seconds."""

    sse_read_timeout: NotRequired[float]
    """The timeout for the SSE connection, in seconds. Defaults to 5 minutes."""

    auth: NotRequired[Any]
    """Optional authentication handler for the installed MCP SDK's HTTP stack.

    Use ``httpx.Auth`` with MCP v1 or ``httpx2.Auth`` with MCP v2.
    """

    httpx_client_factory: NotRequired[HttpClientFactory]
    """Custom HTTP client factory for the installed MCP SDK's HTTP stack.

    Return ``httpx.AsyncClient`` with MCP v1 or ``httpx2.AsyncClient`` with MCP v2.
    """


class MCPServerSse(_MCPServerWithClientSession):
    """MCP server implementation that uses the HTTP with SSE transport. See the [spec]
    (https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/transports/#http-with-sse)
    for details.
    """

    def __init__(
        self,
        params: MCPServerSseParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
        max_retry_attempts: int = 0,
        retry_backoff_seconds_base: float = 1.0,
        message_handler: MessageHandlerFnT | None = None,
        require_approval: RequireApprovalSetting = None,
        failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
        tool_meta_resolver: MCPToolMetaResolver | None = None,
        custom_data_extractor: MCPToolCustomDataExtractor | None = None,
    ):
        """Create a new MCP server based on the HTTP with SSE transport.

        Args:
            params: The params that configure the server. This includes the URL of the server,
                the headers to send to the server, the timeout for the HTTP request, and the
                timeout for the SSE connection.

            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).

            name: A readable name for the server. If not provided, we'll create one from the
                URL.

            client_session_timeout_seconds: The MCP ClientSession read timeout. Positive finite
                values representable by `datetime.timedelta` and at least one microsecond set a
                timeout; `None` and `0` disable it. Other values are rejected during server
                construction.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
            max_retry_attempts: Number of times to retry failed list_tools/call_tool calls.
                Defaults to no retries.
            retry_backoff_seconds_base: The base delay, in seconds, for exponential
                backoff between retries.
            message_handler: Optional handler invoked for session messages as delivered by the
                ClientSession.
            require_approval: Approval policy for tools on this server. Accepts "always"/"never",
                a dict of tool names to those values, or an object with always/never tool lists.
            failure_error_function: Optional function used to convert MCP tool failures into
                a model-visible error message. If explicitly set to None, tool errors will be
                raised instead of converted. If left unset, the agent-level configuration (or
                SDK default) will be used.
            tool_meta_resolver: Optional callable that produces MCP request metadata (`_meta`) for
                tool calls. It is invoked by the Agents SDK before calling `call_tool`.
            custom_data_extractor: Optional callable that produces SDK-only custom data for
                emitted MCP tool output items.
        """
        super().__init__(
            cache_tools_list=cache_tools_list,
            client_session_timeout_seconds=client_session_timeout_seconds,
            tool_filter=tool_filter,
            use_structured_content=use_structured_content,
            max_retry_attempts=max_retry_attempts,
            retry_backoff_seconds_base=retry_backoff_seconds_base,
            message_handler=message_handler,
            require_approval=require_approval,
            failure_error_function=failure_error_function,
            tool_meta_resolver=tool_meta_resolver,
            custom_data_extractor=custom_data_extractor,
        )

        self.params = params
        self._name = name or f"sse: {self.params['url']}"

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[MCPStreamTransport]:
        """Create the streams for the server."""
        kwargs: dict[str, Any] = {
            "url": self.params["url"],
            "headers": self.params.get("headers", None),
            "timeout": self.params.get("timeout", 5),
            "sse_read_timeout": self.params.get("sse_read_timeout", 60 * 5),
        }
        if MCP_V2:
            _validate_v2_http_auth(self.params.get("auth"))
            factory = (
                self.params.get("httpx_client_factory") or _create_default_streamable_http_client
            )
            kwargs["httpx_client_factory"] = _validated_v2_http_client_factory(factory)
            if "auth" in self.params:
                kwargs["auth"] = self.params["auth"]
            return sse_client(**kwargs)

        if "auth" in self.params:
            kwargs["auth"] = self.params["auth"]
        kwargs["httpx_client_factory"] = (
            self.params.get("httpx_client_factory") or _create_default_streamable_http_client
        )
        return sse_client(**kwargs)

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name


class MCPServerStreamableHttpParams(TypedDict):
    """Mirrors the params in `mcp.client.streamable_http.streamablehttp_client`."""

    url: str
    """The URL of the server."""

    headers: NotRequired[dict[str, str]]
    """The headers to send to the server."""

    timeout: NotRequired[timedelta | float]
    """The timeout for the HTTP request. Defaults to 5 seconds."""

    sse_read_timeout: NotRequired[timedelta | float]
    """The timeout for the SSE connection, in seconds. Defaults to 5 minutes."""

    terminate_on_close: NotRequired[bool]
    """Terminate on close"""

    httpx_client_factory: NotRequired[HttpClientFactory]
    """Custom HTTP client factory for the installed MCP SDK's HTTP stack.

    Return ``httpx.AsyncClient`` with MCP v1 or ``httpx2.AsyncClient`` with MCP v2.
    """

    auth: NotRequired[Any]
    """Optional authentication handler for the installed MCP SDK's HTTP stack.

    Use ``httpx.Auth`` with MCP v1 or ``httpx2.Auth`` with MCP v2.
    """

    ignore_initialized_notification_failure: NotRequired[bool]
    """Whether to ignore failures when sending the best-effort
    ``notifications/initialized`` POST.

    Defaults to ``False``. When set to ``True``, initialized-notification failures are
    logged and ignored so subsequent requests on the same transport can continue. This
    option requires MCP Python SDK v1; MCP v2 rejects it before connecting because its
    public transport API does not expose these failures.
    """


class MCPServerStreamableHttp(_MCPServerWithClientSession):
    """MCP server implementation that uses the Streamable HTTP transport. See the [spec]
    (https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http)
    for details.
    """

    def __init__(
        self,
        params: MCPServerStreamableHttpParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
        max_retry_attempts: int = 0,
        retry_backoff_seconds_base: float = 1.0,
        message_handler: MessageHandlerFnT | None = None,
        require_approval: RequireApprovalSetting = None,
        failure_error_function: ToolErrorFunction | None | _UnsetType = _UNSET,
        tool_meta_resolver: MCPToolMetaResolver | None = None,
        custom_data_extractor: MCPToolCustomDataExtractor | None = None,
    ):
        """Create a new MCP server based on the Streamable HTTP transport.

        Args:
            params: The params that configure the server. This includes the URL of the server,
                the headers to send to the server, the timeout for the HTTP request, the
                timeout for the Streamable HTTP connection, whether we need to
                terminate on close, and an optional custom HTTP client factory.

            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).

            name: A readable name for the server. If not provided, we'll create one from the
                URL.

            client_session_timeout_seconds: The MCP ClientSession read timeout. Positive finite
                values representable by `datetime.timedelta` and at least one microsecond set a
                timeout; `None` and `0` disable it. Other values are rejected during server
                construction.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
            max_retry_attempts: Number of times to retry failed list_tools/call_tool calls.
                Defaults to no retries.
            retry_backoff_seconds_base: The base delay, in seconds, for exponential
                backoff between retries.
            message_handler: Optional handler invoked for session messages as delivered by the
                ClientSession.
            require_approval: Approval policy for tools on this server. Accepts "always"/"never",
                a dict of tool names to those values, or an object with always/never tool lists.
            failure_error_function: Optional function used to convert MCP tool failures into
                a model-visible error message. If explicitly set to None, tool errors will be
                raised instead of converted. If left unset, the agent-level configuration (or
                SDK default) will be used.
            tool_meta_resolver: Optional callable that produces MCP request metadata (`_meta`) for
                tool calls. It is invoked by the Agents SDK before calling `call_tool`.
            custom_data_extractor: Optional callable that produces SDK-only custom data for
                emitted MCP tool output items.
        """
        super().__init__(
            cache_tools_list=cache_tools_list,
            client_session_timeout_seconds=client_session_timeout_seconds,
            tool_filter=tool_filter,
            use_structured_content=use_structured_content,
            max_retry_attempts=max_retry_attempts,
            retry_backoff_seconds_base=retry_backoff_seconds_base,
            message_handler=message_handler,
            require_approval=require_approval,
            failure_error_function=failure_error_function,
            tool_meta_resolver=tool_meta_resolver,
            custom_data_extractor=custom_data_extractor,
        )

        self.params = params
        self._name = name or f"streamable_http: {self.params['url']}"
        self._serialize_session_requests = True

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[MCPStreamTransport]:
        """Create the streams for the server."""
        kwargs: dict[str, Any] = {
            "url": self.params["url"],
            "headers": self.params.get("headers", None),
            "timeout": self.params.get("timeout", 5),
            "sse_read_timeout": self.params.get("sse_read_timeout", 60 * 5),
            "terminate_on_close": self.params.get("terminate_on_close", True),
        }
        httpx_client_factory = self.params.get("httpx_client_factory")
        if MCP_V2:
            if self.params.get("ignore_initialized_notification_failure", False):
                raise UserError(
                    "ignore_initialized_notification_failure is not supported with MCP Python "
                    "SDK v2 because its public transport API does not expose initialized-"
                    "notification failures. Leave it disabled or pin mcp<2."
                )
            _validate_v2_http_auth(self.params.get("auth"))
            on_session_id: Callable[[str], None] | None = None
            if self.session is None:
                self._v2_session_id = None

                def capture_session_id(session_id: str) -> None:
                    self._v2_session_id = session_id

                on_session_id = capture_session_id
                self._get_session_id = lambda: self._v2_session_id
            return _streamablehttp_client_v2(
                **kwargs,
                httpx_client_factory=httpx_client_factory or _create_default_streamable_http_client,
                auth=self.params.get("auth"),
                on_session_id=on_session_id,
            )

        if self.params.get("ignore_initialized_notification_failure", False):
            return _streamablehttp_client_with_transport(
                **kwargs,
                httpx_client_factory=httpx_client_factory or _create_default_streamable_http_client,
                auth=self.params.get("auth"),
                transport_factory=_InitializedNotificationTolerantStreamableHTTPTransport,
            )
        kwargs["httpx_client_factory"] = (
            httpx_client_factory or _create_default_streamable_http_client
        )
        if "auth" in self.params:
            kwargs["auth"] = self.params["auth"]
        return cast(
            AbstractAsyncContextManager[MCPStreamTransport],
            _require_streamablehttp_client_v1()(**kwargs),
        )

    @asynccontextmanager
    async def _isolated_client_session(self):
        read_timeout = _client_session_read_timeout(self.client_session_timeout_seconds)
        async with self._client_session_context(read_timeout) as session:
            yield session

    async def _call_tool_with_session(
        self,
        session: ClientSession,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        if meta is None:
            return await session.call_tool(tool_name, arguments)
        return cast(
            CallToolResult,
            await cast(Any, session).call_tool(tool_name, arguments, meta=meta),
        )

    def _should_retry_in_isolated_session(self, exc: BaseException) -> bool:
        if isinstance(exc, asyncio.CancelledError | ClosedResourceError):
            return True
        if is_http_connect_error(exc) or is_http_timeout_error(exc):
            return True
        if is_http_status_error(exc):
            return http_status_code(exc) >= 500
        if isinstance(exc, MCPError):
            return is_mcp_timeout_error(exc) or is_mcp_connection_closed_error(exc)
        if isinstance(exc, BaseExceptionGroup):
            return bool(exc.exceptions) and all(
                self._should_retry_in_isolated_session(inner) for inner in exc.exceptions
            )
        return False

    async def _call_tool_with_shared_session(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
        *,
        allow_isolated_retry: bool,
    ) -> CallToolResult:
        session = self.session
        assert session is not None
        try:
            return await self._maybe_serialize_request(
                lambda: self._call_tool_with_session(session, tool_name, arguments, meta)
            )
        except BaseException as exc:
            if allow_isolated_retry and self._should_retry_in_isolated_session(exc):
                raise _SharedSessionRequestNeedsIsolation from exc
            raise

    async def _call_tool_with_isolated_retry(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
        *,
        allow_isolated_retry: bool,
    ) -> tuple[CallToolResult, bool]:
        request_task = asyncio.create_task(
            self._call_tool_with_shared_session(
                tool_name,
                arguments,
                meta,
                allow_isolated_retry=allow_isolated_retry,
            )
        )
        try:
            return await asyncio.shield(request_task), False
        except _SharedSessionRequestNeedsIsolation:
            exit_stack = AsyncExitStack()
            try:
                session = await exit_stack.enter_async_context(self._isolated_client_session())
            except asyncio.CancelledError:
                await exit_stack.aclose()
                raise
            except BaseException as exc:
                await exit_stack.aclose()
                raise _IsolatedSessionRetryFailed() from exc
            try:
                try:
                    result = await self._call_tool_with_session(session, tool_name, arguments, meta)
                    return result, True
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    raise _IsolatedSessionRetryFailed() from exc
            finally:
                await exit_stack.aclose()
        except asyncio.CancelledError:
            if not request_task.done():
                request_task.cancel()
            try:
                await request_task
            except BaseException:
                pass
            raise

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")

        transport_error: UserError | None = None
        transport_cause: Exception | None = None
        try:
            self._validate_required_parameters(tool_name=tool_name, arguments=arguments)
            retries_used = 0
            # `retries_used` measures the retry budget, not elapsed backoffs: it is
            # deliberately not advanced while `max_retry_attempts` is -1, and a single
            # isolated-session retry charges it twice. Count backoffs separately so the
            # delay follows the configured schedule in both cases.
            backoffs_taken = 0
            first_attempt = True
            while True:
                if not first_attempt and self.max_retry_attempts != -1:
                    retries_used += 1
                allow_isolated_retry = (
                    self.max_retry_attempts == -1 or retries_used < self.max_retry_attempts
                )
                try:
                    result, used_isolated_retry = await self._call_tool_with_isolated_retry(
                        tool_name,
                        arguments,
                        meta,
                        allow_isolated_retry=allow_isolated_retry,
                    )
                    if used_isolated_retry and self.max_retry_attempts != -1:
                        retries_used += 1
                    return result
                except _IsolatedSessionRetryFailed as exc:
                    retries_used += 1
                    if self.max_retry_attempts != -1 and retries_used >= self.max_retry_attempts:
                        if exc.__cause__ is not None:
                            raise exc.__cause__ from exc
                        raise
                    backoff = self.retry_backoff_seconds_base * (2**backoffs_taken)
                    backoffs_taken += 1
                    await asyncio.sleep(backoff)
                except Exception:
                    if self.max_retry_attempts != -1 and retries_used >= self.max_retry_attempts:
                        raise
                    backoff = self.retry_backoff_seconds_base * (2**backoffs_taken)
                    backoffs_taken += 1
                    await asyncio.sleep(backoff)
                first_attempt = False
        except HTTP_STATUS_ERROR_TYPES as e:
            status_code = http_status_code(e)
            transport_error = UserError(
                f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                f"HTTP error {status_code}"
            )
            transport_cause = _safe_transport_cause(e)
        except HTTP_REQUEST_ERROR_TYPES as e:
            transport_cause = _safe_transport_cause(e)
            if transport_cause is not None and not is_http_connect_error(e):
                raise
            if is_http_connect_error(e):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection lost. The server may have disconnected."
                )
            elif is_http_timeout_error(e):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection timeout."
                )
            else:
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Request failed."
                )
        except BaseExceptionGroup as e:
            http_errors = self._extract_http_errors_from_exception(e)
            if not http_errors:
                raise

            unsafe_http_error = _first_unretainable_transport_error(http_errors)
            http_error = unsafe_http_error or http_errors[0]
            transport_cause = _safe_transport_cause(http_error)
            if is_http_status_error(http_error):
                status_code = http_status_code(http_error)
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    f"HTTP error {status_code}"
                )
            elif is_http_connect_error(http_error):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection lost. The server may have disconnected."
                )
            elif is_http_timeout_error(http_error):
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Connection timeout."
                )
            elif is_http_request_error(http_error):
                if transport_cause is not None:
                    raise
                transport_error = UserError(
                    f"Failed to call tool '{tool_name}' on MCP server '{self._error_name}': "
                    "Request failed."
                )
            else:
                raise
            if transport_cause is None:
                http_errors.clear()
                del http_error
                del unsafe_http_error

        assert transport_error is not None
        self._raise_mapped_transport_error(transport_error, transport_cause)

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name

    @property
    def session_id(self) -> str | None:
        """The legacy MCP session ID assigned by the server, if one is available.

        MCP 2026-07-28 does not use protocol sessions, so this property returns None for a
        modern connection. It also returns None before connection or when a legacy server does
        not issue a session ID. A legacy session ID is stable for this instance's connection and
        can be passed through the Mcp-Session-Id request header when reconnecting to a server that
        supports legacy session resumption.

        Example::

            async with MCPServerStreamableHttp(params={"url": url}) as server:
                session_id = server.session_id

            # In a new worker / process:
            async with MCPServerStreamableHttp(
                params={"url": url, "headers": {"Mcp-Session-Id": session_id}}
            ) as server:
                # Resumes the same server-side session.
                ...
        """
        if self._get_session_id is None:
            return None
        return self._get_session_id()
