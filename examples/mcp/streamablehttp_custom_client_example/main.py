"""Example demonstrating custom httpx_client_factory for MCPServerStreamableHttp.

This example shows how to configure custom HTTP client behavior for MCP StreamableHTTP
connections, including SSL certificates, proxy settings, and custom timeouts.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import time
from typing import Any, cast

import httpx

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


def create_custom_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create a custom HTTP client with specific configurations.

    This function demonstrates how to configure:
    - Custom SSL verification settings
    - Custom timeouts
    - Custom headers
    - Proxy settings (commented out)
    """
    if headers is None:
        headers = {
            "X-Custom-Client": "agents-mcp-example",
            "User-Agent": "OpenAI-Agents-MCP/1.0",
        }
    if timeout is None:
        timeout = httpx.Timeout(60.0, read=120.0)
    if auth is None:
        auth = None
    return httpx.AsyncClient(
        # Disable SSL verification for testing (not recommended for production)
        verify=False,
        # Set custom timeout
        timeout=httpx.Timeout(60.0, read=120.0),
        # Add custom headers that will be sent with every request
        headers=headers,
    )


async def run_with_custom_client(mcp_server: MCPServer):
    """Run the agent with a custom HTTP client configuration."""
    agent = Agent(
        name="Assistant",
        instructions="Use the tools to answer the questions.",
        mcp_servers=[mcp_server],
        model_settings=ModelSettings(tool_choice="required"),
    )

    # Use the `add` tool to add two numbers
    message = "Add these numbers: 7 and 22."
    print(f"Running: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)


async def main():
    """Main function demonstrating different HTTP client configurations."""

    print("=== Example: Custom HTTP Client with SSL disabled and custom headers ===")
    async with MCPServerStreamableHttp(
        name="Streamable HTTP with Custom Client",
        params={
            "url": STREAMABLE_HTTP_URL,
            "httpx_client_factory": create_custom_http_client,
        },
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="Custom HTTP Client Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/logs/trace?trace_id={trace_id}\n")
            await run_with_custom_client(server)


if __name__ == "__main__":
    # Let's make sure the user has uv installed
    if not shutil.which("uv"):
        raise RuntimeError(
            "uv is not installed. Please install it: https://docs.astral.sh/uv/getting-started/installation/"
        )

    # We'll run the Streamable HTTP server in a subprocess. Usually this would be a remote server, but for this
    # demo, we'll run it locally at STREAMABLE_HTTP_URL
    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print(f"Starting Streamable HTTP server at {STREAMABLE_HTTP_URL} ...")

        # Run `uv run server.py` to start the Streamable HTTP server
        env = os.environ.copy()
        env.setdefault("STREAMABLE_HTTP_HOST", STREAMABLE_HTTP_HOST)
        env.setdefault("STREAMABLE_HTTP_PORT", str(STREAMABLE_HTTP_PORT))
        process = subprocess.Popen(["uv", "run", server_file], env=env)
        # Give it 3 seconds to start
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
