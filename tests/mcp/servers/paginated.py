from __future__ import annotations

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    ListPromptsRequest,
    ListPromptsResult,
    ListToolsRequest,
    ListToolsResult,
    Prompt,
    TextContent,
    Tool,
)

server = Server("paginated-test-server")


@server.list_tools()  # type: ignore[misc]
async def list_tools(request: ListToolsRequest) -> ListToolsResult:
    cursor = request.params.cursor if request.params is not None else None
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


@server.list_prompts()  # type: ignore[misc]
async def list_prompts(request: ListPromptsRequest) -> ListPromptsResult:
    cursor = request.params.cursor if request.params is not None else None
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


@server.call_tool()  # type: ignore[misc]
async def call_tool(name: str, arguments: dict[str, object] | None) -> list[TextContent]:
    if name not in {"first_page_tool", "second_page_tool"}:
        raise ValueError(f"Unexpected tool: {name}")
    return [TextContent(type="text", text=f"called:{name}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
