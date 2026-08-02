import os

from mcp.server.mcpserver import MCPServer

STREAMABLE_HTTP_HOST = os.getenv("STREAMABLE_HTTP_HOST", "127.0.0.1")
STREAMABLE_HTTP_PORT = int(os.getenv("STREAMABLE_HTTP_PORT", "8000"))

mcp = MCPServer("FastAPI Example Server")


@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b


@mcp.tool()
def echo(message: str) -> str:
    return f"echo: {message}"


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=STREAMABLE_HTTP_HOST,
        port=STREAMABLE_HTTP_PORT,
    )
