import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from agents import gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


async def run(mcp_server: MCPServerStreamableHttp):
    print(f"Cached tools before invoking tool_list")
    print(mcp_server._tools_list)

    print(f"Cached tools names after invoking list_tools")
    await mcp_server.list_tools()
    cached_tools_list = mcp_server._tools_list
    for tool in cached_tools_list:
        print(f"name: {tool.name}")

    print(f"Cached prompts before invoking list_prompts")
    print(mcp_server._prompts_list)

    print(f"\nCached prompts after invoking list_prompts")
    await mcp_server.list_prompts()
    cached_prompts_list = mcp_server._prompts_list
    for prompt in cached_prompts_list.prompts:
        print(f"name: {prompt.name}")

async def main():
    async with MCPServerStreamableHttp(
        name="Streamable HTTP Python Server",
        cache_tools_list=True,
        cache_prompts_list=True,
        params={
            "url": "http://localhost:8000/mcp",
        },
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="Caching Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            await run(server)


if __name__ == "__main__":
    # Let's make sure the user has uv installed
    if not shutil.which("uv"):
        raise RuntimeError(
            "uv is not installed. Please install it: https://docs.astral.sh/uv/getting-started/installation/"
        )

    # We'll run the Streamable HTTP server in a subprocess. Usually this would be a remote server, but for this
    # demo, we'll run it locally at http://localhost:8000/mcp
    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print("Starting Streamable HTTP server at http://localhost:8000/mcp ...")

        # Run `uv run server.py` to start the Streamable HTTP server
        process = subprocess.Popen(["uv", "run", server_file])
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
