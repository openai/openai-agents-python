from __future__ import annotations

from importlib import import_module
from importlib.metadata import version
from types import ModuleType
from typing import Any, cast

import httpx
from pydantic import AnyUrl


def _major_version(distribution: str) -> int:
    raw_version = version(distribution)
    major, separator, _ = raw_version.partition(".")
    if not separator or not major.isdigit():  # pragma: no cover - package versions are validated
        raise RuntimeError(f"Unsupported {distribution} version: {raw_version}")
    return int(major)


MCP_MAJOR_VERSION = _major_version("mcp")
MCP_V2 = MCP_MAJOR_VERSION >= 2

_mcp_exceptions = import_module("mcp.shared.exceptions")
MCPError = cast(
    type[Exception],
    vars(_mcp_exceptions).get("MCPError") or vars(_mcp_exceptions)["McpError"],
)

MCP_HTTPX: ModuleType = import_module("httpx2") if MCP_V2 else httpx

HTTP_STATUS_ERROR_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.HTTPStatusError, cast(type[Exception], MCP_HTTPX.HTTPStatusError)))
)
HTTP_REQUEST_ERROR_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.RequestError, cast(type[Exception], MCP_HTTPX.RequestError)))
)
HTTP_CONNECT_ERROR_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.ConnectError, cast(type[Exception], MCP_HTTPX.ConnectError)))
)
HTTP_TIMEOUT_ERROR_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.TimeoutException, cast(type[Exception], MCP_HTTPX.TimeoutException)))
)
HTTP_ERROR_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.HTTPError, cast(type[Exception], MCP_HTTPX.HTTPError)))
)
HTTP_INVALID_URL_TYPES: tuple[type[Exception], ...] = tuple(
    dict.fromkeys((httpx.InvalidURL, cast(type[Exception], MCP_HTTPX.InvalidURL)))
)


def create_v2_client(
    transport: Any,
    *,
    read_timeout_seconds: float | None,
    message_handler: Any,
) -> Any:
    if not MCP_V2:  # pragma: no cover - guarded by the caller
        raise RuntimeError("MCP v2 client requested with MCP v1 installed.")
    client_class = vars(import_module("mcp"))["Client"]
    return client_class(
        transport,
        mode="auto",
        cache=None,
        read_timeout_seconds=read_timeout_seconds,
        message_handler=message_handler,
    )


def streamable_http_client_v2(
    url: str,
    *,
    http_client: Any,
    terminate_on_close: bool,
) -> Any:
    if not MCP_V2:  # pragma: no cover - guarded by the caller
        raise RuntimeError("MCP v2 transport requested with MCP v1 installed.")
    module = import_module("mcp.client.streamable_http")
    return module.streamable_http_client(
        url,
        http_client=http_client,
        terminate_on_close=terminate_on_close,
    )


def tool_input_schema(tool: Any) -> dict[str, Any]:
    return cast(dict[str, Any], tool.input_schema if MCP_V2 else tool.inputSchema)


def result_next_cursor(result: Any) -> str | None:
    return cast(str | None, result.next_cursor if MCP_V2 else result.nextCursor)


def clear_result_next_cursor(result: Any, **updates: Any) -> Any:
    updates["next_cursor" if MCP_V2 else "nextCursor"] = None
    return result.model_copy(update=updates)


def result_structured_content(result: Any) -> dict[str, Any] | None:
    return cast(
        dict[str, Any] | None,
        result.structured_content if MCP_V2 else result.structuredContent,
    )


def result_is_error(result: Any) -> bool | None:
    return cast(bool | None, result.is_error if MCP_V2 else result.isError)


def image_mime_type(content: Any) -> str:
    return cast(str, content.mime_type if MCP_V2 else content.mimeType)


def resource_uri(uri: str) -> str | AnyUrl:
    return uri if MCP_V2 else AnyUrl(uri)


def mcp_error_code(error: BaseException) -> int | None:
    if not isinstance(error, MCPError):
        return None
    if MCP_V2:
        return cast(int, cast(Any, error).code)
    error_data = getattr(error, "error", None)
    return cast(int | None, getattr(error_data, "code", None))


def mcp_error_message(error: BaseException) -> str:
    if not isinstance(error, MCPError):
        return str(error)
    if MCP_V2:
        return cast(str, cast(Any, error).message)
    error_data = getattr(error, "error", None)
    return cast(str, getattr(error_data, "message", str(error)))


def mcp_request_timeout_code() -> int:
    return -32001 if MCP_V2 else int(httpx.codes.REQUEST_TIMEOUT)


def is_mcp_timeout_error(error: BaseException) -> bool:
    return mcp_error_code(error) == mcp_request_timeout_code()


def is_mcp_connection_closed_error(error: BaseException) -> bool:
    if not MCP_V2:
        return False
    connection_closed = int(vars(import_module("mcp_types"))["CONNECTION_CLOSED"])
    return mcp_error_code(error) == connection_closed


def is_http_status_error(error: BaseException) -> bool:
    return isinstance(error, HTTP_STATUS_ERROR_TYPES)


def is_http_request_error(error: BaseException) -> bool:
    return isinstance(error, HTTP_REQUEST_ERROR_TYPES)


def is_http_connect_error(error: BaseException) -> bool:
    return isinstance(error, HTTP_CONNECT_ERROR_TYPES)


def is_http_timeout_error(error: BaseException) -> bool:
    return isinstance(error, HTTP_TIMEOUT_ERROR_TYPES)


def is_http_transport_error(error: BaseException) -> bool:
    return is_http_status_error(error) or is_http_request_error(error)


def http_status_code(error: BaseException) -> int:
    return cast(int, cast(Any, error).response.status_code)


def http_reason_phrase(error: BaseException) -> str:
    return cast(str, cast(Any, error).response.reason_phrase)
