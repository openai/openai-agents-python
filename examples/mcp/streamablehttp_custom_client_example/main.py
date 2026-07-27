"""Example demonstrating custom HTTP configuration for MCPServerStreamableHttp.

With MCP Python SDK v2, the underlying transport uses httpx2 and the
``httpx_client_factory`` parameter is no longer supported.  To customise HTTP
behaviour pass the ``headers``, ``timeout``, ``sse_read_timeout``, or ``auth``
(``httpx2.Auth``) keys in ``MCPServerStreamableHttpParams``.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import time
from typing import Any, cast

import httpx2  # noqa: F401 — available for the auth example in main()

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServer, MCPServerStreamableHttp
from agents.model_settings import ModelSettings

STREAMABLE_HTTP_HOST = os.getenv("STREAMABLE_HTTP_HOST", "127.0.0.1")


def _choose_port() -> int:
    env_port = os.getenv("STREAMABLE_HTTP_PORT")
    if env_port:
        return int(env_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((STREAMABLE_HTTP_HOST, 0))
        address = cast(tuple[str, int], s.getsockname())
        return address[1]


STREAMABLE_HTTP_PORT = _choose_port()
os.environ.setdefault("STREAMABLE_HTTP_PORT", str(STREAMABLE_HTTP_PORT))
STREAMABLE_HTTP_URL = f"http://{STREAMABLE_HTTP_HOST}:{STREAMABLE_HTTP_PORT}/mcp"


async def run_with_server(mcp_server: MCPServer):
    agent = Agent(
        name="Assistant",
        instructions="Use the tools to answer the questions.",
        mcp_servers=[mcp_server],
        model_settings=ModelSettings(tool_choice="required"),
    )
    message = "Add these numbers: 7 and 22."
    print(f"Running: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)


async def main():
    """Demonstrate custom HTTP configuration for StreamableHTTP (mcp SDK v2)."""

    print("=== Example: StreamableHTTP with custom headers and timeout ===")

    # Use ``headers``, ``timeout``, ``sse_read_timeout``, and ``auth``
    # (``httpx2.Auth`` instance) to customise the underlying httpx2 client.
    # The ``httpx_client_factory`` parameter was removed in mcp SDK v2.
    async with MCPServerStreamableHttp(
        name="Streamable HTTP – custom config",
        params={
            "url": STREAMABLE_HTTP_URL,
            "headers": {
                "X-Custom-Client": "agents-mcp-example",
                "User-Agent": "OpenAI-Agents-MCP/2.0",
            },
            "timeout": 60.0,
            "sse_read_timeout": 120.0,
            # To add authentication, pass an httpx2.Auth instance:
            # "auth": httpx2.BasicAuth(username="user", password="secret"),
        },
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="Custom HTTP Config Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/logs/trace?trace_id={trace_id}\n")
            await run_with_server(server)


if __name__ == "__main__":
    if not shutil.which("uv"):
        raise RuntimeError(
            "uv is not installed. Please install it: https://docs.astral.sh/uv/getting-started/installation/"
        )

    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print(f"Starting Streamable HTTP server at {STREAMABLE_HTTP_URL} ...")

        env = os.environ.copy()
        env.setdefault("STREAMABLE_HTTP_HOST", STREAMABLE_HTTP_HOST)
        env.setdefault("STREAMABLE_HTTP_PORT", str(STREAMABLE_HTTP_PORT))
        process = subprocess.Popen(["uv", "run", server_file], env=env)
        time.sleep(3)

        print("Streamable HTTP server started. Running example...\n\n")
    except Exception as e:
        print(f"Error starting Streamable HTTP server: {e}")
        exit(1)

    try:
        asyncio.run(main())
    finally:
        if process:
            process.terminate()
