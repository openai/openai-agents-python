from __future__ import annotations

from importlib.metadata import version
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListPromptsRequest,
    ListPromptsResult as _ListPromptsResult,
    ListToolsRequest,
    ListToolsResult as _ListToolsResult,
    Prompt,
    TextContent,
    Tool as _Tool,
)


class ListPromptsResult(_ListPromptsResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class ListToolsResult(_ListToolsResult):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class Tool(_Tool):
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


MCP_V2 = int(version("mcp").partition(".")[0]) >= 2


def tools_page(cursor: str | None) -> ListToolsResult:
    if cursor is None:
        return ListToolsResult(
            tools=[
                Tool(
                    name="first_page_tool",
                    inputSchema={"type": "object", "properties": {}},
                )
            ],
            nextCursor="",
        )
    if cursor == "":
        return ListToolsResult(
            tools=[
                Tool(
                    name="second_page_tool",
                    inputSchema={"type": "object", "properties": {}},
                )
            ],
        )
    raise ValueError(f"Unexpected tools cursor: {cursor}")


def prompts_page(cursor: str | None) -> ListPromptsResult:
    if cursor is None:
        return ListPromptsResult(
            prompts=[Prompt(name="first_page_prompt")],
            nextCursor="",
            _meta={"page": "first"},
        )
    if cursor == "":
        return ListPromptsResult(
            prompts=[Prompt(name="second_page_prompt")],
            _meta={"page": "second"},
        )
    raise ValueError(f"Unexpected prompts cursor: {cursor}")


def tool_result(name: str) -> CallToolResult:
    if name not in {"first_page_tool", "second_page_tool"}:
        raise ValueError(f"Unexpected tool: {name}")
    return CallToolResult(content=[TextContent(type="text", text=f"called:{name}")])


if MCP_V2:

    async def list_tools_v2(_context: Any, params: Any) -> ListToolsResult:
        return tools_page(params.cursor if params is not None else None)

    async def list_prompts_v2(_context: Any, params: Any) -> ListPromptsResult:
        return prompts_page(params.cursor if params is not None else None)

    async def call_tool_v2(_context: Any, params: Any) -> CallToolResult:
        return tool_result(params.name)

    server = Server(
        "paginated-test-server",
        on_list_tools=list_tools_v2,
        on_list_prompts=list_prompts_v2,
        on_call_tool=call_tool_v2,
    )
else:
    server = Server("paginated-test-server")

    @server.list_tools()  # type: ignore[attr-defined, misc]
    async def list_tools_v1(request: ListToolsRequest) -> ListToolsResult:
        return tools_page(request.params.cursor if request.params is not None else None)

    @server.list_prompts()  # type: ignore[attr-defined, misc]
    async def list_prompts_v1(request: ListPromptsRequest) -> ListPromptsResult:
        return prompts_page(request.params.cursor if request.params is not None else None)

    @server.call_tool()  # type: ignore[attr-defined, misc]
    async def call_tool_v1(name: str, arguments: dict[str, object] | None) -> list[TextContent]:
        del arguments
        return tool_result(name).content  # type: ignore[return-value]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
