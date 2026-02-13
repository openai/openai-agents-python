---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）对应用如何向语言模型暴露工具和上下文进行了标准化。摘自官方文档：

> MCP 是一种开放协议，用于标准化应用如何向 LLM 提供上下文。可以把 MCP 想象成 AI 应用的 USB-C 端口。正如 USB-C 提供了一种标准化方式，将你的设备连接到各种外设和配件，MCP 也提供了一种标准化方式，将 AI 模型连接到不同的数据源和工具。

Agents Python SDK 支持多种 MCP 传输方式。这使你能够复用现有 MCP server，或自行构建 MCP server，以向智能体暴露由文件系统、HTTP 或连接器支撑的工具。

## MCP 集成选择

在将 MCP server 接入智能体之前，请先决定工具调用应在哪里执行，以及你可以访问哪些传输方式。下表总结了 Python SDK 支持的选项。

| 需求 | 推荐选项 |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用一个可公开访问的 MCP server | 通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 使用 **Hosted MCP server tools** |
| 连接到你在本地或远程运行的 Streamable HTTP server | 通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 使用 **Streamable HTTP MCP servers** |
| 与实现了基于 Server-Sent Events 的 HTTP server 通信 | 通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 使用 **HTTP with SSE MCP servers** |
| 启动本地进程并通过 stdin/stdout 通信 | 通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 使用 **stdio MCP servers** |

下文将逐一介绍每个选项、如何配置，以及何时应优先选择某种传输方式。

## 智能体级 MCP 配置

除了选择传输方式外，你还可以通过设置 `Agent.mcp_config` 来调整 MCP 工具的准备方式。

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
    },
)
```

说明：

- `convert_schemas_to_strict` 为尽力而为。如果某个 schema 无法转换，将使用原始 schema。
- `failure_error_function` 控制 MCP 工具调用失败如何呈现给模型。
- 当未设置 `failure_error_function` 时，SDK 使用默认的工具错误格式化器。
- server 级别的 `failure_error_function` 会覆盖该 server 的 `Agent.mcp_config["failure_error_function"]`。

## 1. Hosted MCP server tools

Hosted tools 将整个工具往返过程下沉到 OpenAI 的基础设施中。你的代码无需列出并调用工具；相反，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会将一个 server 标签（以及可选的 connector 元数据）转发给 Responses API。模型会列出远程 server 的工具并调用它们，而无需额外回调到你的 Python 进程。Hosted tools 当前适用于支持 Responses API 的 hosted MCP 集成的 OpenAI 模型。

### 基础 hosted MCP tool

通过向智能体的 `tools` 列表添加一个 [`HostedMCPTool`][agents.tool.HostedMCPTool] 来创建 hosted tool。`tool_config` 字典与发送到 REST API 的 JSON 保持一致：

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

Hosted server 会自动暴露其工具；你无需将其添加到 `mcp_servers`。

### 流式传输 hosted MCP 结果

Hosted tools 以与工具调用完全相同的方式支持流式传输结果。使用 `Runner.run_streamed` 在模型仍在工作时消费增量 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选的审批流程

如果某个 server 可以执行敏感操作，你可以在每次工具执行前要求人工或程序化审批。在 `tool_config` 中配置 `require_approval`，可以是单一策略（`"always"`、`"never"`），也可以是将工具名映射到策略的字典。若要在 Python 内做出决策，请提供 `on_approval_request` 回调。

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

该回调可以是同步或异步的，并且会在模型需要审批数据以继续运行时被调用。

### 基于 connector 的 hosted servers

Hosted MCP 也支持 OpenAI connectors。无需指定 `server_url`，只需提供 `connector_id` 和 access token。Responses API 负责鉴权，hosted server 将暴露 connector 的工具。

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

可运行的完整 hosted tool 示例（包括流式传输、审批与 connectors）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP servers

当你希望自行管理网络连接时，请使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你控制传输层，或希望在自有基础设施内运行 server 并保持低延迟时，Streamable HTTP server 是理想选择。

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

构造函数还接受其他选项：

- `client_session_timeout_seconds` 控制 HTTP 读取超时。
- `use_structured_content` 控制是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 与 `call_tool()` 添加自动重试。
- `tool_filter` 允许你只暴露部分工具（参见 [工具过滤](#tool-filtering)）。
- `require_approval` 为本地 MCP 工具启用 human-in-the-loop 审批策略。
- `failure_error_function` 自定义模型可见的 MCP 工具失败消息；将其设为 `None` 则改为抛出错误。
- `tool_meta_resolver` 在 `call_tool()` 之前为每次调用注入 MCP `_meta` 负载。

### 本地 MCP servers 的审批策略

`MCPServerStdio`、`MCPServerSse` 与 `MCPServerStreamableHttp` 都支持 `require_approval`。

支持形式：

- 对所有工具使用 `"always"` 或 `"never"`。
- `True` / `False`（等价于 always/never）。
- 按工具映射，例如 `{"delete_file": "always", "read_file": "never"}`。
- 分组对象：
  `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

如需完整的暂停/恢复流程，请参阅 [Human-in-the-loop](human_in_the_loop.md) 以及 `examples/mcp/get_all_mcp_tools_example/main.py`。

### 使用 `tool_meta_resolver` 的逐调用元数据

当你的 MCP server 期望在 `_meta` 中接收请求元数据（例如租户 ID 或 trace 上下文）时，请使用 `tool_meta_resolver`。下面的示例假设你向 `Runner.run(...)` 传入一个 `dict` 作为 `context`。

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

如果你的 run context 是 Pydantic 模型、dataclass 或自定义类，请改为使用属性访问来读取租户 ID。

### MCP 工具输出：文本与图像

当 MCP 工具返回图像内容时，SDK 会自动将其映射为图像工具输出条目。混合文本/图像的响应会以输出项列表的形式转发，因此智能体可以用与消费常规工具调用的图像输出相同的方式来消费 MCP 图像结果。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP 项目已弃用 Server-Sent Events 传输方式。新集成请优先使用 Streamable HTTP 或 stdio，SSE 仅用于遗留 server。

如果 MCP server 实现了 HTTP with SSE 传输方式，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与 Streamable HTTP server 完全相同。

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

对于以本地子进程方式运行的 MCP server，请使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动该进程，保持管道开启，并在上下文管理器退出时自动关闭。此选项适用于快速概念验证，或当 server 仅暴露命令行入口时。

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

## 5. MCP server 管理器

当你有多个 MCP server 时，请使用 `MCPServerManager` 预先连接它们，并将已连接的子集暴露给你的智能体。

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

关键行为：

- 当 `drop_failed_servers=True`（默认）时，`active_servers` 仅包含连接成功的 server。
- 失败会记录在 `failed_servers` 与 `errors` 中。
- 设置 `strict=True` 可在首次连接失败时直接抛出异常。
- 调用 `reconnect(failed_only=True)` 重试失败的 server，或调用 `reconnect(failed_only=False)` 重启所有 server。
- 使用 `connect_timeout_seconds`、`cleanup_timeout_seconds` 和 `connect_in_parallel` 来调整生命周期行为。

## 工具过滤

每个 MCP server 都支持工具过滤，以便你仅暴露智能体所需的函数。过滤既可在构造时进行，也可在每次运行时动态进行。

### 静态工具过滤

使用 [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] 配置简单的允许/阻止列表：

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

当同时提供 `allowed_tool_names` 与 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除被阻止的工具。

### 动态工具过滤

对于更复杂的逻辑，可传入一个可调用对象，该对象接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext]。该可调用对象可以是同步或异步的，并在工具应被暴露时返回 `True`。

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

过滤上下文提供当前的 `run_context`、请求工具的 `agent` 以及 `server_name`。

## Prompts

MCP server 也可以提供 prompts，用于动态生成智能体指令。支持 prompts 的 server 会暴露两个方法：

- `list_prompts()` 枚举可用的 prompt 模板。
- `get_prompt(name, arguments)` 获取一个具体 prompt，并可选地带参数。

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

## 缓存

每次智能体运行都会对每个 MCP server 调用 `list_tools()`。远程 server 可能引入明显延迟，因此所有 MCP server 类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时才将其设为 `True`。若需稍后强制刷新列表，请在 server 实例上调用 `invalidate_tools_cache()`。

## 追踪

[Tracing](./tracing.md) 会自动捕获 MCP 活动，包括：

1. 调用 MCP server 列出工具的请求。
2. 工具调用中的 MCP 相关信息。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的 hosted MCP 演示，包括审批与 connectors。