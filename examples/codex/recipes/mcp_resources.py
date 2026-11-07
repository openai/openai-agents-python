"""Read resources from a trusted HTTP MCP server without a model turn.

Install the matching SDK wheel, then run:
    python -m examples.codex.recipes.mcp_resources URL URI

The server may advertise resources without tools. Each list call returns one
page; pass its nextCursor to the same method to request the next page. Read
results preserve text, base64 blobs and metadata as data. The SDK does not add
these results to model context. Resource calls require an idle Session.
"""

import argparse
import asyncio
import json

from agents import Agent, HttpMCP


async def read(url: str, uri: str) -> None:
    server = HttpMCP(name="documents", url=url, tools=[])
    async with Agent(instructions="Read selected documents.", tools=[server]).session() as session:
        resources = await session.list_mcp_resources("documents")
        print(json.dumps(resources, indent=2))
        templates = await session.list_mcp_resource_templates("documents")
        print(json.dumps(templates, indent=2))
        contents = await session.read_mcp_resource("documents", uri)
        print(json.dumps(contents, indent=2))
        # Continue explicitly when another page is wanted:
        if "nextCursor" in resources:
            following = await session.list_mcp_resources("documents", resources["nextCursor"])
            print(json.dumps(following, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Trusted streamable HTTP MCP endpoint")
    parser.add_argument("uri", help="Resource URI served by this endpoint")
    args = parser.parse_args()
    asyncio.run(read(args.url, args.uri))
