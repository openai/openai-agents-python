# Model context protocol (MCP)

The [Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) standardises how applications expose tools and
context to language models. From the official documentation:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

The Agents Python SDK understands multiple MCP transports. This lets you reuse existing MCP servers or build your own to expose filesystem, HTTP, or connector backed tools to an agent.

!!! warning "Trust MCP servers before connecting"

    MCP tools can expose data from the model context and perform actions with the credentials you provide. Connect only to servers you trust, use least-privilege credentials, keep access tokens in authorization fields or headers rather than URLs, and require approval for sensitive operations. See the [OpenAI MCP security guidance](https://developers.openai.com/api/docs/guides/tools-connectors-mcp#risks-and-safety).

## Choosing an MCP integration

Before wiring an MCP server into an agent decide where the tool calls should execute and which transports you can reach. The matrix below summarises the options that the Python SDK supports.

| What you need                                                                        | Recommended option                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Let OpenAI's Responses API call a publicly reachable MCP server on the model's behalf| **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| Connect to Streamable HTTP servers that you run locally or remotely                  | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Talk to servers that implement HTTP with Server-Sent Events                          | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| Launch a local process and communicate over stdin/stdout                             | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

The sections below walk through each option, how to configure it, and when to prefer one transport over another.

## MCP Python SDK v1 and v2

The Agents SDK supports both major versions of the `mcp` Python package through the dependency range `mcp>=1.19.0,<3`. The installed `mcp` package version is separate from the MCP protocol version negotiated with a server. The Agents SDK detects the installed package major version and adapts stdio, SSE, and Streamable HTTP connections automatically, so ordinary server configuration does not need a version switch.

When MCP Python SDK v2 is installed, the Agents SDK creates the v2 `mcp.Client` with `mode="auto"` around the configured local transport. The client first sends a `server/discover` probe at the newest protocol version supported by the installed MCP SDK. A modern server answers the probe, and the client adopts the result. If an older server does not support `server/discover`, the client falls back to the legacy `initialize` handshake and uses the protocol version negotiated there. Installing MCP Python SDK v2 therefore does not force every connection to use the newest MCP protocol version. See the MCP Python SDK's [protocol version negotiation guide](https://py.sdk.modelcontextprotocol.io/protocol-versions/).

Most applications should let their dependency resolver select a compatible version. If your application must stay on one major version, add an explicit constraint alongside `openai-agents`:

```bash
# MCP Python SDK v1
pip install "mcp>=1.19.0,<2"

# MCP Python SDK v2
pip install "mcp>=2,<3"
```

HTTP transport customization must use the HTTP stack owned by the installed MCP package:

| Customization | MCP Python SDK v1 | MCP Python SDK v2 |
| --- | --- | --- |
| `params["auth"]` | `httpx.Auth` | `httpx2.Auth` |
| `params["httpx_client_factory"]` return value | `httpx.AsyncClient` | `httpx2.AsyncClient` |
| `MCPServerStreamableHttp` `params["ignore_initialized_notification_failure"] = True` | Supported | Not supported; rejected before connecting |

Use an `Authorization` header when possible, as shown in the Streamable HTTP example below; an `Authorization` header works unchanged with both package versions. When an application supplies `params["auth"]` or `params["httpx_client_factory"]`, those values must use the HTTP types for the installed `mcp` package major version. When an application sets `MCPServerStreamableHttp`'s `params["ignore_initialized_notification_failure"] = True`, the application must keep `mcp<2` or disable the option before upgrading.

These local `mcp` dependency requirements do not apply to [`HostedMCPTool`][agents.tool.HostedMCPTool] because the OpenAI Responses API owns the remote MCP connection.

## Agent-level MCP configuration

In addition to choosing a transport, you can tune how MCP tools are prepared by setting `Agent.mcp_config`.

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    mcp_servers=[server],
    mcp_config={
        # Try to convert MCP tool schemas to strict JSON schema.
        "convert_schemas_to_strict": True,
        # If None, MCP tool failures are raised as exceptions instead of
        # returning model-visible error text.
        "failure_error_function": None,
        # Prefix local MCP tool names with their server name.
        "include_server_in_tool_names": True,
    },
)
```

Notes:

- `convert_schemas_to_strict` is best-effort. If a schema cannot be converted, the original schema is used.
- `failure_error_function` controls how MCP tool call failures are surfaced to the model.
- When `failure_error_function` is unset, the SDK uses the default tool error formatter.
- Server-level `failure_error_function` overrides `Agent.mcp_config["failure_error_function"]` for that server.
- `include_server_in_tool_names` is opt-in. When enabled, each local MCP tool is exposed to the model with a deterministic server-prefixed name, which helps avoid collisions when multiple MCP servers publish tools with the same name. Generated names are ASCII-safe, stay within the name-length limit for `FunctionTool` instances, and do not collide with the configured names of local `FunctionTool` instances or enabled handoffs on the same agent. The SDK still invokes the original MCP tool name on the original server.

## Shared patterns across transports

After you choose a transport, most integrations need the same follow-up decisions:

- How to expose only a subset of tools ([Tool filtering](#tool-filtering)).
- Whether the server also provides reusable prompts ([Prompts](#prompts)).
- Whether `list_tools()` should be cached ([Caching](#caching)).
- How MCP activity appears in traces ([Tracing](#tracing)).

For local MCP servers (`MCPServerStdio`, `MCPServerSse`, `MCPServerStreamableHttp`), approval policies and per-call `_meta` payloads are also shared concepts. The Streamable HTTP section shows the most complete examples, and the same patterns apply to the other local transports.

## 1. Hosted MCP server tools

Hosted tools push the entire tool round-trip into OpenAI's infrastructure. Instead of your code listing and calling tools, the [`HostedMCPTool`][agents.tool.HostedMCPTool] forwards a server label (and optional connector metadata) to the Responses API. The model lists the remote server's tools and invokes them without an extra callback to your Python process. Hosted tools currently work with OpenAI models that support the Responses API's hosted MCP integration.

### Basic hosted MCP tool

Create a hosted tool by adding a [`HostedMCPTool`][agents.tool.HostedMCPTool] to the agent's `tools` list. The `tool_config`
dict mirrors the JSON you would send to the REST API:

```python
import asyncio

from agents import Agent, HostedMCPTool, Runner

async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Use the DeepWiki hosted MCP server to inspect openai/openai-agents-python.",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "deepwiki",
                    "server_url": "https://mcp.deepwiki.com/mcp",
                    "require_approval": "never",
                }
            )
        ],
    )

    result = await Runner.run(
        agent,
        "Which language is the repository openai/openai-agents-python written in?",
    )
    print(result.final_output)

asyncio.run(main())
```

The hosted server exposes its tools automatically; you do not add it to `mcp_servers`.

If you want hosted tool search to load a hosted MCP server lazily, set `tool_config["defer_loading"] = True` and add [`ToolSearchTool`][agents.tool.ToolSearchTool] to the agent. This is supported only on OpenAI Responses models. See [Tools](tools.md#hosted-tool-search) for the complete tool-search setup and constraints.

### Streaming hosted MCP results

Hosted tools support streaming results in exactly the same way as function tools. Use `Runner.run_streamed` to
consume incremental MCP output while the model is still working:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

If a server can perform sensitive operations you can require human or programmatic approval before each tool execution. Configure `require_approval` in the `tool_config` with either a single policy (`"always"`, `"never"`) or a dict mapping tool names to policies. To make the decision inside Python, provide an `on_approval_request` callback.

```python
from agents import MCPToolApprovalFunctionResult, MCPToolApprovalRequest

SAFE_TOOLS = {"read_wiki_structure", "read_wiki_contents", "ask_question"}

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
                "server_label": "deepwiki",
                "server_url": "https://mcp.deepwiki.com/mcp",
                "require_approval": "always",
            },
            on_approval_request=approve_tool,
        )
    ],
)
```

The callback can be synchronous or asynchronous and is invoked whenever the model needs approval data to keep running.

### Connector-backed hosted servers

Hosted MCP also supports OpenAI connectors. Instead of specifying a `server_url`, supply a `connector_id` and an access token. The Responses API handles authentication and the hosted server exposes the connector's tools.

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

Fully working hosted tool samples—including streaming, approvals, and connectors—live in [`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp).

## 2. Streamable HTTP MCP servers

When you want to manage the network connection yourself, use [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]. Streamable HTTP servers are ideal when you control the transport or want to run the server inside your own infrastructure while keeping latency low.

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

- `client_session_timeout_seconds` controls MCP ClientSession read timeouts. Positive finite values representable by `datetime.timedelta` and at least one microsecond set a finite timeout; `None` and `0` disable it. Other values are rejected when the server is constructed.
- `use_structured_content` toggles whether `tool_result.structured_content` is preferred over textual output.
- `max_retry_attempts` and `retry_backoff_seconds_base` add automatic retries for `list_tools()` and `call_tool()`.
- `tool_filter` lets you expose only a subset of tools (see [Tool filtering](#tool-filtering)).
- `require_approval` enables human-in-the-loop approval policies on local MCP tools.
- `failure_error_function` customizes model-visible MCP tool failure messages; set it to `None` to raise errors instead.
- `tool_meta_resolver` injects per-call MCP `_meta` payloads before `call_tool()`.

### Approval policies for local MCP servers

`MCPServerStdio`, `MCPServerSse`, and `MCPServerStreamableHttp` all accept `require_approval`.

Supported forms:

- `"always"` or `"never"` for all tools.
- `True` requires approval for all tools, and `False` requires approval for none (equivalent to `"always"` and `"never"`, respectively).
- A per-tool map, for example `{"delete_file": "always", "read_file": "never"}`.
- A grouped object: `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`.

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

For a full pause/resume flow, see [Human-in-the-loop](human_in_the_loop.md) and `examples/mcp/get_all_mcp_tools_example/main.py`.

### Per-call metadata with `tool_meta_resolver`

Use `tool_meta_resolver` when your MCP server expects request metadata in `_meta` (for example, tenant IDs or trace context). The example below assumes you pass a `dict` as `context` to `Runner.run(...)`.

```python
from agents.mcp import MCPServerStreamableHttp, MCPToolMetaContext


def resolve_meta(context: MCPToolMetaContext) -> dict[str, str] | None:
    run_context_data = context.run_context.context or {}
    tenant_id = run_context_data.get("tenant_id")
    if tenant_id is None:
        return None
    return {"tenant_id": str(tenant_id), "source": "agents-sdk"}


server = MCPServerStreamableHttp(
    name="Metadata-aware MCP",
    params={"url": "http://localhost:8000/mcp"},
    tool_meta_resolver=resolve_meta,
)
```

If your run context is a Pydantic model, dataclass, or custom class, read the tenant ID with attribute access instead.

### MCP tool outputs: text, images, and other content

When an MCP result uses its content blocks, the SDK forwards text content as text output and maps image content to image-type entries in the tool output. For other MCP content block types, including audio and resource blocks, the SDK forwards a text output whose value is the block's valid JSON serialization. Responses that contain multiple content blocks are forwarded as a list of output items. If `use_structured_content=True` selects a non-empty, non-error `structuredContent` payload, that structured payload takes precedence over these content blocks. Missing or empty structured content falls back to the content blocks.

## 3. HTTP with SSE MCP servers

!!! warning

    The MCP project has deprecated the Server-Sent Events transport. Prefer Streamable HTTP or stdio for new integrations and keep SSE only for legacy servers.

If the MCP server implements the HTTP with SSE transport, instantiate [`MCPServerSse`][agents.mcp.server.MCPServerSse]. Apart from the transport, the API is identical to the Streamable HTTP server.

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

For MCP servers that run as local subprocesses, use [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]. The SDK spawns the process, keeps the pipes open, and closes them automatically when the context manager exits. This option is helpful for quick proofs of concept or when the server only exposes a command line entry point.

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

## 5. MCP server manager

When you have multiple MCP servers, use `MCPServerManager` to connect them up front and expose the successfully connected subset of those servers to your agents. See the [MCPServerManager API reference](ref/mcp/manager.md) for constructor options and reconnect behavior.

```python
from agents import Agent, Runner
from agents.mcp import MCPServerManager, MCPServerStreamableHttp

servers = [
    MCPServerStreamableHttp(name="calendar", params={"url": "http://localhost:8000/mcp"}),
    MCPServerStreamableHttp(name="docs", params={"url": "http://localhost:8001/mcp"}),
]

async with MCPServerManager(servers) as manager:
    agent = Agent(
        name="Assistant",
        instructions="Use MCP tools when they help.",
        mcp_servers=manager.active_servers,
    )
    result = await Runner.run(agent, "Which MCP tools are available?")
    print(result.final_output)
```

Key behaviors:

- `active_servers` includes only successfully connected servers when `drop_failed_servers=True` (the default).
- If the input iterable repeats the same server object, the manager owns that server once: `all_servers` and `active_servers` contain one entry, and connection and cleanup run once for that server.
- Failures are tracked in `failed_servers` and `errors`.
- Set `strict=True` to raise on the first connection failure.
- Call `reconnect(failed_only=True)` to retry failed servers, or `reconnect(failed_only=False)` to restart all servers.
- Calls to `connect_all()`, `reconnect()`, and `cleanup_all()` are serialized. If one lifecycle operation is already running, another lifecycle operation waits for it to finish instead of connecting or cleaning up the same servers concurrently.
- Set `connect_timeout_seconds`, `cleanup_timeout_seconds`, and `connect_in_parallel` to tune lifecycle behavior. Both lifecycle timeouts default to 10 seconds. They accept positive finite seconds, or `None` to disable them, and are validated both during construction and assignment; zero is rejected because it would create an immediate deadline.

## Common server capabilities

The sections below apply across MCP server transports (with the exact API surface depending on the server class).

## Tool filtering

Each MCP server supports tool filters so that you can expose only the functions that your agent needs. Filtering can happen at construction time or dynamically per run.

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

When both `allowed_tool_names` and `blocked_tool_names` are supplied the SDK applies the allow-list first and then removes any blocked tools from the remaining set.

### Dynamic tool filtering

For more elaborate logic pass a callable that receives a [`ToolFilterContext`][agents.mcp.ToolFilterContext]. The callable can be synchronous or asynchronous and returns `True` when the tool should be exposed.

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

## Tool guardrails

Local MCP server classes accept `tool_input_guardrails` and `tool_output_guardrails`. The SDK attaches these server-wide guardrails to every MCP tool that remains after filtering. Input guardrails can prevent the MCP server call and supply replacement content, while output guardrails inspect the converted MCP result before the SDK sends that result back to the model. These guardrails use the same function-tool execution pipeline, approval ordering, result tracking, and tripwire exceptions described in [Tool guardrails](guardrails.md#tool-guardrails).

```python
import json

from agents import ToolGuardrailFunctionOutput
from agents.decorators import tool_input_guardrail
from agents.mcp import MCPServerStdio


@tool_input_guardrail
def block_secret_arguments(data):
    arguments = json.loads(data.context.tool_arguments or "{}")
    if "secret" in arguments:
        return ToolGuardrailFunctionOutput.reject_content(
            "Remove secrets before calling this MCP tool."
        )
    return ToolGuardrailFunctionOutput.allow()


filesystem_server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
    tool_input_guardrails=[block_secret_arguments],
)
```

This configuration applies only to tools exposed by local MCP server objects such as `MCPServerStdio`, `MCPServerSse`, and `MCPServerStreamableHttp`. It does not add client-side tool guardrails to [`HostedMCPTool`][agents.tool.HostedMCPTool], which the Responses API executes as a hosted tool.

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

## Pagination

The built-in local MCP server classes automatically follow `nextCursor` when listing tools and prompts. `list_tools()` collects the complete tool list before applying filters or populating its cache, and `list_prompts()` returns one combined result with `nextCursor=None`. If a later page fails or a server repeats a cursor, the operation raises an error instead of exposing or caching partial results.

Resources remain explicitly paginated. Pass the `nextCursor` from `list_resources()` or `list_resource_templates()` back as the `cursor` argument to retrieve the next page.

## Caching

Every agent run calls `list_tools()` on each MCP server. Remote servers can introduce noticeable latency, so all of the MCP server classes expose a `cache_tools_list` option. Set it to `True` only if you are confident that the tool definitions do not change frequently. To force a fresh list later, call `invalidate_tools_cache()` on the server instance.

When caching is enabled, each `list_tools()` result contains detached copies of the cached tool definitions, including nested input schemas. Dynamic tool-filter callbacks also inspect detached copies. Mutating a returned tool or a tool received by a filter therefore does not change the server's cached schema or later `list_tools()` results.

## Tracing

[Tracing](./tracing.md) automatically captures MCP activity, including:

1. Calls to the MCP server to list tools.
2. MCP-related information on tool calls.

![MCP Tracing Screenshot](./assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – the specification and design guides.
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – runnable stdio, SSE, and Streamable HTTP samples.
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – complete hosted MCP demonstrations including approvals and connectors.
