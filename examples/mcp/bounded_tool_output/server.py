"""Synthetic reports for the local stdio example."""

from mcp.types import CallToolResult

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

server = MCPServer("Reports")


@server.tool(name="read.report")
async def read_report(kind: str) -> CallToolResult:
    """Read a sample inspection report."""
    detail = "WARN: synthetic detail " * 4
    if kind == "audio":
        return CallToolResult.model_validate(
            {"content": [{"type": "audio", "mimeType": "audio/wav", "data": "QUJD"}]}
        )
    if kind == "ticket":
        return CallToolResult.model_validate(
            {"content": [], "structuredContent": {"ticket": "site"}}
        )
    if kind in ("text", "brief"):
        return CallToolResult.model_validate(
            {
                "content": [
                    {"type": "text", "text": "Sample reading. " * (12 if kind == "text" else 1)}
                ]
            }
        )
    if kind in ("illustrated", "combined"):
        return CallToolResult.model_validate(
            {
                "structuredContent": {"report": detail * 3} if kind == "combined" else None,
                "content": [
                    {"type": "text", "text": detail},
                    {
                        "type": "image",
                        "mimeType": "image/gif",
                        "data": "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
                    },
                    {"type": "text", "text": detail},
                ],
            }
        )
    return CallToolResult.model_validate(
        {
            "content": [
                {
                    "type": "text",
                    "text": "Fallback report"
                    if kind == "structured"
                    else "Report unavailable. " * 10,
                }
            ],
            "structuredContent": {"report": detail * 2},
            "_meta": {"private": detail},
            "isError": kind != "structured",
        }
    )


if __name__ == "__main__":
    server.run()
