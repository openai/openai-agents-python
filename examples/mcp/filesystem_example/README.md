# MCP Filesystem Example

This example uses the [filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem), running locally via `npx`.

Run it via:

```
uv run python examples/mcp/filesystem_example/main.py
```

## Details

The example uses the `MCPServerStdio` class from `agents.mcp`, with the command:

```bash
npx -y "@modelcontextprotocol/server-filesystem" <samples_directory>
```

It's only given access to the `sample_files` directory adjacent to the example, which contains some sample data.

Under the hood:

1. The server is spun up in a subprocess, and exposes a bunch of tools like `list_directory()`, `read_file()`, etc.
2. We add the server instance to the Agent via `mcp_agents`.
3. Each time the agent runs, we call out to the MCP server to fetch the list of tools via `server.list_tools()`.
4. If the LLM chooses to use an MCP tool, we call the MCP server to run the tool via `server.run_tool()`.
