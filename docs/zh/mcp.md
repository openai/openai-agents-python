---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用如何向语言模型暴露工具和上下文。摘自官方文档：

> MCP 是一个开放协议，用于标准化应用向 LLM 提供上下文的方式。可以把 MCP 想象成 AI 应用的 USB-C 端口。正如 USB-C 提供了一种标准化方式，将你的设备连接到各种外设和配件，MCP 也提供了一种标准化方式，将 AI 模型连接到不同的数据源和工具。

Agents Python SDK 支持多种 MCP 传输方式。这让你能够复用现有的 MCP 服务，或自行构建服务，以向智能体暴露由文件系统、HTTP 或连接器支持的工具。

## MCP 集成选择

在将 MCP 服务接入智能体之前，先决定工具调用应在哪里执行，以及你能触达哪些传输方式。下表总结了 Python SDK 支持的选项。

| 你需要的能力                                                                         | 推荐选项                                              |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用一个可公网访问的 MCP 服务                       | 通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 使用 **Hosted MCP server tools** |
| 连接你在本地或远程运行的 Streamable HTTP 服务                                         | 通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 使用 **Streamable HTTP MCP servers** |
| 与实现了带 Server-Sent Events 的 HTTP 的服务通信                                      | 通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 使用 **HTTP with SSE MCP servers** |
| 启动本地进程并通过 stdin/stdout 通信                                                  | 通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 使用 **stdio MCP servers** |

下面将分别介绍每种选项、如何配置，以及何时应优先选择某种传输方式。

## 1. Hosted MCP server tools

Hosted 工具将整个工具往返过程都交由 OpenAI 基础设施处理。你的代码不再负责列出和调用工具，而是由 [`HostedMCPTool`][agents.tool.HostedMCPTool] 将服务标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远程服务的工具并调用它们，无需额外回调到你的 Python 进程。Hosted 工具目前适用于支持 Responses API 的 hosted MCP 集成的 OpenAI 模型。

### 基本 hosted MCP 工具

通过将 [`HostedMCPTool`][agents.tool.HostedMCPTool] 添加到智能体的 `tools` 列表来创建 hosted 工具。`tool_config` dict 对应你会发送到 REST API 的 JSON：

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

Hosted 服务会自动暴露其工具；你无需将其添加到 `mcp_servers`。

### 流式传输 hosted MCP 结果

Hosted 工具以与工具调用完全相同的方式支持流式输出。将 `stream=True` 传给 `Runner.run_streamed`，即可在模型仍在工作时消费增量 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选审批流程

如果服务可能执行敏感操作，你可以在每次工具执行前要求人工或程序化审批。在 `tool_config` 中配置 `require_approval`：可以是单一策略（`"always"`、`"never"`），也可以是将工具名称映射到策略的 dict。若要在 Python 内做出决策，请提供 `on_approval_request` 回调。

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

该回调可以是同步或异步，并会在模型需要审批数据以继续运行时被调用。

### 由连接器支持的 hosted 服务

Hosted MCP 也支持 OpenAI connectors。无需指定 `server_url`，而是提供 `connector_id` 和访问令牌。Responses API 负责处理鉴权，hosted 服务会暴露连接器的工具。

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

完整可运行的 hosted 工具示例（包括流式传输、审批和连接器）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP servers

当你希望自行管理网络连接时，请使用 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。在你可控传输方式，或希望在自有基础设施中运行服务并保持低延迟时，Streamable HTTP 服务是理想选择。

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
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 和 `call_tool()` 增加自动重试。
- `tool_filter` 允许你只暴露工具子集（见 [工具过滤](#tool-filtering)）。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP 项目已弃用 Server-Sent Events 传输方式。新集成应优先使用 Streamable HTTP 或 stdio，SSE 仅用于遗留服务。

如果 MCP 服务实现了带 SSE 的 HTTP 传输方式，请实例化 [`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与 Streamable HTTP 服务完全一致。

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

对于作为本地子进程运行的 MCP 服务，请使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动该进程，保持管道开启，并在上下文管理器退出时自动关闭。该选项适用于快速概念验证，或服务仅提供命令行入口的场景。

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

## 5. MCP 服务管理器

当你有多个 MCP 服务时，使用 `MCPServerManager` 预先连接它们，并将已连接的子集暴露给你的智能体。

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

- 当 `drop_failed_servers=True`（默认）时，`active_servers` 仅包含连接成功的服务。
- 失败会记录在 `failed_servers` 和 `errors` 中。
- 设置 `strict=True` 可在首次连接失败时抛出异常。
- 调用 `reconnect(failed_only=True)` 以重试失败服务，或 `reconnect(failed_only=False)` 以重启所有服务。
- 使用 `connect_timeout_seconds`、`cleanup_timeout_seconds` 和 `connect_in_parallel` 来调整生命周期行为。

## 工具过滤

每个 MCP 服务都支持工具过滤器，以便你只暴露智能体需要的函数。过滤可以在构造时进行，也可以在每次运行时动态进行。

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

当同时提供 `allowed_tool_names` 和 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除被阻止的工具。

### 动态工具过滤

对于更复杂的逻辑，传入一个可调用对象，它接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext]。该可调用对象可以是同步或异步，并在工具应被暴露时返回 `True`。

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

过滤上下文会暴露当前 `run_context`、请求工具的 `agent` 以及 `server_name`。

## Prompts

MCP 服务也可以提供 prompts，用于动态生成智能体指令。支持 prompts 的服务会暴露两个方法：

- `list_prompts()` 枚举可用的 prompt 模板。
- `get_prompt(name, arguments)` 获取一个具体 prompt（可选带参数）。

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

每次智能体运行都会对每个 MCP 服务调用 `list_tools()`。远程服务可能引入明显延迟，因此所有 MCP 服务类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时，才将其设为 `True`。若稍后需要强制刷新列表，请在服务实例上调用 `invalidate_tools_cache()`。

## 追踪

[Tracing](./tracing.md) 会自动捕获 MCP 活动，包括：

1. 调用 MCP 服务以列出工具。
2. 工具调用中的 MCP 相关信息。

![MCP 追踪截图](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 和 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的 hosted MCP 演示，包括审批与连接器。