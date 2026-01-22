import os

from mcp.server.fastmcp import FastMCP

STREAMABLE_HTTP_HOST = os.getenv("STREAMABLE_HTTP_HOST", "127.0.0.1")
STREAMABLE_HTTP_PORT = int(os.getenv("STREAMABLE_HTTP_PORT", "8000"))

mcp = FastMCP(
    "FastAPI Example Server",
    host=STREAMABLE_HTTP_HOST,
    port=STREAMABLE_HTTP_PORT,
)


@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b


@mcp.tool()
def echo(message: str) -> str:
    return f"echo: {message}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
