# MCP Streamable HTTP Remote Example

Python port of the JS `examples/mcp/streamable-http-example.ts`. It connects to DeepWiki over the Streamable HTTP transport (`https://mcp.deepwiki.com/mcp`) and lets the agent use those tools.

Run it with:

```bash
uv run python examples/mcp/streamable_http_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.

## Public web search with Parallel

For public web search and page extraction, connect to [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp) using the same Streamable HTTP client. The anonymous endpoint needs no Parallel account, API key, or authorization header. Free access is rate limited; `OPENAI_API_KEY` is still required for the agent's model calls.

Save the following as `parallel_search.py` and run `uv run python parallel_search.py` from your SDK environment:

```python
import asyncio

from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp


async def main():
    async with MCPServerStreamableHttp(
        name="Parallel Search",
        params={"url": "https://search.parallel.ai/mcp", "timeout": 30},
        client_session_timeout_seconds=60,
    ) as server:
        agent = Agent(
            name="Web research assistant",
            instructions="Use web_search to find sources and web_fetch to read pages. Cite URLs.",
            mcp_servers=[server],
        )
        result = await Runner.run(
            agent,
            "Find the official Python asyncio documentation and explain TaskGroup.",
        )
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Adding the connected server to `mcp_servers` makes `web_search` and `web_fetch` available to the agent. The agent can call them while answering a request without a separate confirmation for each call. Queries, requested URLs, and any supplied objectives or context are sent to Parallel; tool results enter the agent's model context. Remove the server from `mcp_servers` to stop exposing these tools to the agent.
