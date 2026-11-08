"""Preview prompts from a trusted native HTTP MCP server without model work.

Install the matching SDK wheel, then run:
    python -m examples.codex.recipes.mcp_prompts URL NAME --argument language=Python

Listing exhausts pages or raises without returning a partial inventory. Native
catalog limits are 100 pages, 2,048 prompts and 64 KiB per cursor. Collection
uses the configured execution timeout, defaulting to 60 seconds. Returned
messages remain caller data; the SDK does not add them to model context.
Prompt operations require an idle Session and can use a server with no tools.
"""

import argparse
import asyncio
import json

from agents import Agent, HttpMCP


async def preview(url: str, name: str, arguments: dict[str, str] | None) -> None:
    server = HttpMCP(name="catalog", url=url, tools=[])
    async with Agent(instructions="Preview selected prompts.", tools=[server]).session() as session:
        print(json.dumps(await session.list_mcp_prompts("catalog"), indent=2))
        result = await session.get_mcp_prompt("catalog", name, arguments)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Trusted streamable HTTP MCP endpoint")
    parser.add_argument("name", help="Prompt name served by this endpoint")
    parser.add_argument("--argument", action="append", metavar="KEY=VALUE")
    options = parser.parse_args()
    arguments = None
    if options.argument is not None:
        arguments = {}
        for argument in options.argument:
            key, separator, value = argument.partition("=")
            if not separator:
                parser.error("--argument requires KEY=VALUE")
            arguments[key] = value
    asyncio.run(preview(options.url, options.name, arguments))
