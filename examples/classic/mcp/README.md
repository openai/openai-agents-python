# Classic MCP examples

These examples connect Classic agents to Model Context Protocol servers. Complete [Classic example setup](../README.md#setup) and set `OPENAI_API_KEY` before model calls. The local HTTP server examples use the repository's locked MCP v2 environment; the SDK client itself supports MCP v1 and v2.

| Task | Example | Additional setup |
| --- | --- | --- |
| Read bundled files | [Filesystem](filesystem_example/README.md) | Node.js, npm, and `npx`; the first run downloads a server package. |
| Inspect a local Git repository | [Git](git_example/README.md) | `uvx`, Git, and a repository path. |
| Prefetch tools | [Get all tools](get_all_mcp_tools_example/README.md) | `npx`. |
| Restrict available tools | [Tool filtering](tool_filter_example/README.md) | `npx`. |
| Start a local SSE server | [SSE](sse_example/README.md) | Repository development environment. |
| Override the SSE endpoint | [Remote SSE](sse_remote_example/README.md) | Optional `MCP_SSE_REMOTE_URL`; the default starts a local server. |
| Start a local HTTP server | [Streamable HTTP](streamablehttp_example/README.md) | Repository development environment. |
| Connect to DeepWiki | [Remote Streamable HTTP](streamable_http_remote_example/README.md) | Access to the remote MCP endpoint. |
| Customize HTTP clients | [Client factory](streamablehttp_custom_client_example/README.md) | Repository MCP v2 and `httpx2` environment. |
| Load instructions from MCP prompts | [Prompt server](prompt_server/README.md) | Repository development environment. |
| Manage connections in FastAPI | [MCP manager](manager_example/README.md) | The documented FastAPI dependency; offline smoke test available. |

The tool-filtering and tool-prefetch examples automatically approve interruptions to exercise the approval path. This demonstration does not collect human consent. The Git example exposes the tools supplied by its MCP server to the model; use a disposable repository when testing possible changes.
