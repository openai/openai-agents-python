import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerSse
from agents.model_settings import ModelSettings

SSE_HOST = os.getenv("SSE_HOST", "127.0.0.1")
REMOTE_SSE_URL = os.getenv("MCP_SSE_REMOTE_URL")


def _choose_port() -> int:
    env_port = os.getenv("SSE_PORT")
    if env_port:
        return int(env_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((SSE_HOST, 0))
        address = cast(tuple[str, int], sock.getsockname())
        return address[1]


@contextmanager
def local_sse_server() -> Iterator[str]:
    if not shutil.which("uv"):
        raise RuntimeError(
            "uv is not installed. Please install it: "
            "https://docs.astral.sh/uv/getting-started/installation/"
        )

    sse_port = _choose_port()
    sse_url = f"http://{SSE_HOST}:{sse_port}/sse"
    server_file = Path(__file__).resolve().parents[1] / "sse_example" / "server.py"

    print(f"Starting local SSE server at {sse_url} ...", flush=True)
    env = os.environ.copy()
    env.setdefault("SSE_HOST", SSE_HOST)
    env["SSE_PORT"] = str(sse_port)
    process: subprocess.Popen[Any] | None = None

    try:
        process = subprocess.Popen(["uv", "run", str(server_file)], env=env)
        time.sleep(3)
        yield sse_url
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def run(url: str, name: str) -> None:
    async with MCPServerSse(
        name=name,
        params={
            "url": url,
            "timeout": 5,
            "sse_read_timeout": 30,
        },
    ) as server:
        agent = Agent(
            name="SSE Assistant",
            instructions="Use the available MCP tools to answer the user.",
            mcp_servers=[server],
            model_settings=ModelSettings(tool_choice="required"),
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="SSE MCP Server Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            result = await Runner.run(agent, "Use the MCP add tool to add 7 and 22.")
            print(result.final_output)


async def main() -> None:
    if REMOTE_SSE_URL:
        print(f"Connecting to remote SSE server at {REMOTE_SSE_URL} ...", flush=True)
        await run(REMOTE_SSE_URL, "Remote SSE Server")
        return

    print(
        "MCP_SSE_REMOTE_URL is not set; using the bundled local SSE server for this demo.",
        flush=True,
    )
    with local_sse_server() as url:
        await run(url, "Local SSE Server")


if __name__ == "__main__":
    asyncio.run(main())
