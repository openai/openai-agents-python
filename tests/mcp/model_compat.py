from __future__ import annotations

from typing import Any, cast

from mcp import Tool as _Tool
from mcp.types import (
    AudioContent as _AudioContent,
    CallToolResult as _CallToolResult,
    ImageContent as _ImageContent,
    InitializeResult as _InitializeResult,
    JSONRPCMessage as _JSONRPCMessage,
    ListPromptsResult as _ListPromptsResult,
    ListResourceTemplatesResult as _ListResourceTemplatesResult,
    ListToolsResult as _ListToolsResult,
    Resource as _Resource,
    ResourceTemplate as _ResourceTemplate,
    TextResourceContents as _TextResourceContents,
)

from agents.mcp._compat import MCP_V2, MCPError


# MCP v1 and v2 accept their wire-format aliases at runtime, but expose different constructor
# signatures to static type checkers. Keep alias-based fixture construction in one test-only module.
class Tool(_Tool):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class AudioContent(_AudioContent):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class CallToolResult(_CallToolResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class ImageContent(_ImageContent):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class InitializeResult(_InitializeResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


def JSONRPCMessage(*args: Any, **kwargs: Any) -> Any:
    return cast(Any, _JSONRPCMessage)(*args, **kwargs)


class ListPromptsResult(_ListPromptsResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class ListResourceTemplatesResult(_ListResourceTemplatesResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class ListToolsResult(_ListToolsResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class Resource(_Resource):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class ResourceTemplate(_ResourceTemplate):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class TextResourceContents(_TextResourceContents):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


def create_mcp_error(code: int, message: str, data: Any = None) -> Exception:
    if MCP_V2:
        return cast(Exception, cast(Any, MCPError)(code=code, message=message, data=data))
    from mcp.types import ErrorData

    return cast(Exception, cast(Any, MCPError)(ErrorData(code=code, message=message, data=data)))
