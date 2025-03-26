# MCP Git Example

This example uses the [git MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/git), running locally via `uvx`.

Run it via:

```
uv run python examples/mcp/git_example/main.py
```

## Details

The example uses the `MCPServerStdio` class from `agents.mcp`, with the command:

```bash
uvx mcp-server-git
```

Prior to running the agent, the user is prompted to provide a local directory path to their git repo. Using that, the Agent can invoke Git MCP tools like `git_log` to inspect the git commit log.

Under the hood:

1. The server is spun up in a subprocess, and exposes a bunch of tools like `git_log()`
2. We add the server instance to the Agent via `mcp_agents`.
3. Each time the agent runs, we call out to the MCP server to fetch the list of tools via `server.list_tools()`. The result is cached.
4. If the LLM chooses to use an MCP tool, we call the MCP server to run the tool via `server.run_tool()`.
