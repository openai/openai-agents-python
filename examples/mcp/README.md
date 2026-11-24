# MCP examples

These examples connect agents to Model Context Protocol servers. Follow the [example setup](../README.md) and configure `OPENAI_API_KEY` before live model calls. Each linked README gives the server and client commands.

| Task | Example | Extra setup |
| --- | --- | --- |
| Read bundled files | [Filesystem](filesystem_example/README.md) | Node.js, npm, and `npx`; the first run downloads a server package. |
| Inspect a Git repository | [Git](git_example/README.md) | `uvx`, Git, and a repository path. |
| Prefetch tools | [Get all tools](get_all_mcp_tools_example/README.md) | `npx`. |
| Restrict available tools | [Tool filtering](tool_filter_example/README.md) | `npx`. |
| Retrieve large reports privately | [Bounded tool output](bounded_tool_output/README.md) | Runs offline with a local server and no API key. |
| Start or configure an SSE server | [SSE](sse_example/README.md) and [remote SSE](sse_remote_example/README.md) | Local development environment or configured endpoint. |
| Use Streamable HTTP | [Local server](streamablehttp_example/README.md), [remote server](streamable_http_remote_example/README.md), and [custom client](streamablehttp_custom_client_example/README.md) | See each README for dependencies and endpoints. |
| Load MCP prompts | [Prompt server](prompt_server/README.md) | Local development environment. |
| Manage connections in FastAPI | [MCP manager](manager_example/README.md) | FastAPI dependency; offline smoke test available. |

The tool-filtering example automatically approves interruptions. The tool-prefetch example asks for approval interactively and defaults to approval in suite auto mode. The Git server exposes its tools to the model; use a disposable repository when testing possible changes.
