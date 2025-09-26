# Model context protocol (MCP)

The [Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) standardises how applications expose tools and
context to language models. From the official documentation:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

The Agents Python SDK understands multiple MCP transports. This lets you reuse existing MCP servers or build your own to expose
filesystem, HTTP, or connector backed tools to an agent.

## Choosing an MCP integration

Before wiring an MCP server into an agent decide where the tool calls should execute and which transports you can reach. The
matrix below summarises the options that the Python SDK supports.

| What you need                                                                        | Recommended option                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Let OpenAI's Responses API call a publicly reachable MCP server on the model's behalf| **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| Connect to Streamable HTTP servers that you run locally or remotely                  | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Talk to servers that implement HTTP with Server-Sent Events                          | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| Launch a local process and communicate over stdin/stdout                             | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

The sections below walk through each option, how to configure it, and when to prefer one transport over another.

## 1. Hosted MCP server tools

Hosted tools push the entire tool round-trip into OpenAI's infrastructure. Instead of your code listing and calling tools, the
[`HostedMCPTool`][agents.tool.HostedMCPTool] forwards a server label (and optional connector metadata) to the Responses API. The
model lists the remote server's tools and invokes them without an extra callback to your Python process. Hosted tools currently
work with OpenAI models that support the Responses API's hosted MCP integration.

### Basic hosted MCP tool

Create a hosted tool by adding a [`HostedMCPTool`][agents.tool.HostedMCPTool] to the agent's `tools` list. The `tool_config`
dict mirrors the JSON you would send to the REST API:

```python
import asyncio

from agents import Agent, HostedMCPTool, Runner

async def main() -> None:
    agent = Agent(
        name="Assistant",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "gitmcp",
                    "server_url": "https://gitmcp.io/openai/codex",
                    "require_approval": "never",
                }
            )
        ],
    )

    result = await Runner.run(agent, "Which language is this repository written in?")
    print(result.final_output)

asyncio.run(main())
```

The hosted server exposes its tools automatically; you do not add it to `mcp_servers`.

### Streaming hosted MCP results

Hosted tools support streaming results in exactly the same way as function tools. Pass `stream=True` to `Runner.run_streamed` to
consume incremental MCP output while the model is still working:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

If a server can perform sensitive operations you can require human or programmatic approval before each tool execution. Configure
`require_approval` in the `tool_config` with either a single policy (`"always"`, `"never"`) or a dict mapping tool names to
policies. To make the decision inside Python, provide an `on_approval_request` callback.

```python
from agents import MCPToolApprovalFunctionResult, MCPToolApprovalRequest

SAFE_TOOLS = {"read_project_metadata"}

def approve_tool(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
    if request.data.name in SAFE_TOOLS:
        return {"approve": True}
    return {"approve": False, "reason": "Escalate to a human reviewer"}

agent = Agent(
    name="Assistant",
    tools=[
        HostedMCPTool(
            tool_config={
                "type": "mcp",
                "server_label": "gitmcp",
                "server_url": "https://gitmcp.io/openai/codex",
                "require_approval": "always",
            },
            on_approval_request=approve_tool,
        )
    ],
)
```

The callback can be synchronous or asynchronous and is invoked whenever the model needs approval data to keep running.

### Connector-backed hosted servers

Hosted MCP also supports OpenAI connectors. Instead of specifying a `server_url`, supply a `connector_id` and an access token. The
Responses API handles authentication and the hosted server exposes the connector's tools.

```python
import os

HostedMCPTool(
    tool_config={
        "type": "mcp",
        "server_label": "google_calendar",
        "connector_id": "connector_googlecalendar",
        "authorization": os.environ["GOOGLE_CALENDAR_AUTHORIZATION"],
        "require_approval": "never",
    }
)
```

Fully working hosted tool samples—including streaming, approvals, and connectors—live in
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp).

## 2. Streamable HTTP MCP servers

When you want to manage the network connection yourself, use
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]. Streamable HTTP servers are ideal when you control the
transport or want to run the server inside your own infrastructure while keeping latency low.

```python
import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp
from agents.model_settings import ModelSettings

async def main() -> None:
    token = os.environ["MCP_SERVER_TOKEN"]
    async with MCPServerStreamableHttp(
        name="Streamable HTTP Python Server",
        params={
            "url": "http://localhost:8000/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout": 10,
        },
        cache_tools_list=True,
        max_retry_attempts=3,
    ) as server:
        agent = Agent(
            name="Assistant",
            instructions="Use the MCP tools to answer the questions.",
            mcp_servers=[server],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(agent, "Add 7 and 22.")
        print(result.final_output)

asyncio.run(main())
```

The constructor accepts additional options:

- `client_session_timeout_seconds` controls HTTP read timeouts.
- `use_structured_content` toggles whether `tool_result.structured_content` is preferred over textual output.
- `max_retry_attempts` and `retry_backoff_seconds_base` add automatic retries for `list_tools()` and `call_tool()`.
- `tool_filter` lets you expose only a subset of tools (see [Tool filtering](#tool-filtering)).

## 3. HTTP with SSE MCP servers

If the MCP server implements the HTTP with SSE transport, instantiate
[`MCPServerSse`][agents.mcp.server.MCPServerSse]. Apart from the transport, the API is identical to the Streamable HTTP server.

```python

from agents import Agent, Runner
from agents.model_settings import ModelSettings
from agents.mcp import MCPServerSse

workspace_id = "demo-workspace"

async with MCPServerSse(
    name="SSE Python Server",
    params={
        "url": "http://localhost:8000/sse",
        "headers": {"X-Workspace": workspace_id},
    },
    cache_tools_list=True,
) as server:
    agent = Agent(
        name="Assistant",
        mcp_servers=[server],
        model_settings=ModelSettings(tool_choice="required"),
    )
    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)
```

## 4. stdio MCP servers

For MCP servers that run as local subprocesses, use [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]. The SDK spawns the
process, keeps the pipes open, and closes them automatically when the context manager exits. This option is helpful for quick
proofs of concept or when the server only exposes a command line entry point.

```python
from pathlib import Path
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

current_dir = Path(__file__).parent
samples_dir = current_dir / "sample_files"

async with MCPServerStdio(
    name="Filesystem Server via npx",
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
) as server:
    agent = Agent(
        name="Assistant",
        instructions="Use the files in the sample directory to answer questions.",
        mcp_servers=[server],
    )
    result = await Runner.run(agent, "List the files available to you.")
    print(result.final_output)
```

## Tool filtering

Each MCP server supports tool filters so that you can expose only the functions that your agent needs. Filtering can happen at
construction time or dynamically per run.

### Static tool filtering

Use [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] to configure simple allow/block lists:

```python
from pathlib import Path

from agents.mcp import MCPServerStdio, create_static_tool_filter

samples_dir = Path("/path/to/files")

filesystem_server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
    tool_filter=create_static_tool_filter(allowed_tool_names=["read_file", "write_file"]),
)
```

When both `allowed_tool_names` and `blocked_tool_names` are supplied the SDK applies the allow-list first and then removes any
blocked tools from the remaining set.

### Dynamic tool filtering

For more elaborate logic pass a callable that receives a [`ToolFilterContext`][agents.mcp.ToolFilterContext]. The callable can be
synchronous or asynchronous and returns `True` when the tool should be exposed.

```python
from pathlib import Path

from agents.mcp import MCPServerStdio, ToolFilterContext

samples_dir = Path("/path/to/files")

async def context_aware_filter(context: ToolFilterContext, tool) -> bool:
    if context.agent.name == "Code Reviewer" and tool.name.startswith("danger_"):
        return False
    return True

async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(samples_dir)],
    },
    tool_filter=context_aware_filter,
) as server:
    ...
```

The filter context exposes the active `run_context`, the `agent` requesting the tools, and the `server_name`.

## Prompts

MCP servers can also provide prompts that dynamically generate agent instructions. Servers that support prompts expose two
methods:

- `list_prompts()` enumerates the available prompt templates.
- `get_prompt(name, arguments)` fetches a concrete prompt, optionally with parameters.

```python
from agents import Agent

prompt_result = await server.get_prompt(
    "generate_code_review_instructions",
    {"focus": "security vulnerabilities", "language": "python"},
)
instructions = prompt_result.messages[0].content.text

agent = Agent(
    name="Code Reviewer",
    instructions=instructions,
    mcp_servers=[server],
)
```

## Caching

Every agent run calls `list_tools()` on each MCP server. Remote servers can introduce noticeable latency, so all of the MCP
server classes expose a `cache_tools_list` option. Set it to `True` only if you are confident that the tool definitions do not
change frequently. To force a fresh list later, call `invalidate_tools_cache()` on the server instance.

## Tracing

[Tracing](./tracing.md) automatically captures MCP activity, including:

1. Calls to the MCP server to list tools.
2. MCP-related information on tool calls.

![MCP Tracing Screenshot](./assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – the specification and design guides.
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – runnable stdio, SSE, and Streamable HTTP samples.
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – complete hosted MCP demonstrations including approvals and connectors.
