import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from pydantic import AnyUrl

from agents import gen_trace_id, trace
from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp.types import EmptyResult, ListResourcesResult, ReadResourceResult

async def list_resources(mcp_server: MCPServer) -> ListResourcesResult:
    """List available resources"""
    resources_result = await mcp_server.list_resources()
    print("\n### Resources ###")
    for resource in resources_result.resources:
        print(f"name: {resource.name}, description: {resource.description}")

async def list_resource_templates(mcp_server: MCPServer) -> ListResourcesResult:
    """List available resources templates"""
    resources_templates_result = await mcp_server.list_resource_templates()
    print("\n### Resource Templates ###")
    for resource in resources_templates_result.resourceTemplates:
        print(f"name: {resource.name}, description: {resource.description}")

async def read_resource(mcp_server: MCPServer, uri: AnyUrl) -> ReadResourceResult:
    resource = await mcp_server.read_resource(uri)
    print(resource.contents[0].text)

async def main():
    async with MCPServerStreamableHttp(
        name="Simple Prompt Server",
        params={"url": "http://localhost:8000/mcp"},
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="Simple Prompt Demo", trace_id=trace_id):
            print(f"Trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")

            await list_resources(server)
            await list_resource_templates(server)
            await read_resource(server, AnyUrl("docs://api/reference"))

if __name__ == "__main__":
    if not shutil.which("uv"):
        raise RuntimeError("uv is not installed")

    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print("Starting Simple Resources Server...")
        process = subprocess.Popen(["uv", "run", server_file])
        time.sleep(3)
        print("Server started\n")
    except Exception as e:
        print(f"Error starting server: {e}")
        exit(1)

    try:
        asyncio.run(main())
    finally:
        if process:
            process.terminate()
            print("Server terminated.")
