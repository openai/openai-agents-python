---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) 规范了应用如何向语言模型公开工具和上下文。来自官方文档：

> MCP 是一个开放协议，用于标准化应用向 LLM 提供上下文的方式。可以把 MCP 想象成 AI
> 应用的 USB-C 端口。就像 USB-C 提供了一种标准化方式来将你的设备连接到各种外设和配件一样，MCP
> 也提供了一种标准化方式来将 AI 模型连接到不同的数据源和工具。

Agents Python SDK 支持多种 MCP 传输方式。这使你能够复用现有 MCP 服务，或构建自己的服务，将文件系统、HTTP 或连接器支持的工具公开给智能体。

## MCP 集成选择

在将 MCP 服务接入智能体之前，请先决定工具调用应在哪里执行，以及你可以访问哪些传输方式。下表汇总了 Python SDK 支持的选项。

| 你的需求                                                                        | 推荐选项                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用一个可公开访问的 MCP 服务| 通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 使用**托管 MCP 服务工具** |
| 连接到你在本地或远程运行的 Streamable HTTP 服务                  | 通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 使用 **Streamable HTTP MCP 服务** |
| 与实现了带 Server-Sent Events 的 HTTP 的服务通信                          | 通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 使用**带 SSE 的 HTTP MCP 服务** |
| 启动本地进程并通过 stdin/stdout 通信                             | 通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 使用 **stdio MCP 服务** |

以下各节将介绍每种选项、配置方式，以及何时应优先选择某种传输方式。

## 智能体级 MCP 配置

除了选择传输方式之外，你还可以通过设置 `Agent.mcp_config` 来调优 MCP 工具的准备方式。

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

说明：

- `convert_schemas_to_strict` 是尽力而为的。如果某个 schema 无法转换，将使用原始 schema。
- `failure_error_function` 控制 MCP 工具调用失败如何呈现给模型。
- 当未设置 `failure_error_function` 时，SDK 会使用默认的工具错误格式化器。
- 服务级别的 `failure_error_function` 会覆盖该服务上的 `Agent.mcp_config["failure_error_function"]`。
- `include_server_in_tool_names` 是可选开启的。启用后，每个本地 MCP 工具都会以确定性的、带服务前缀的名称公开给模型，这有助于在多个 MCP 服务发布同名工具时避免冲突。生成的名称是 ASCII 安全的，会保持在函数工具名称长度限制内，并避免与同一智能体上的现有本地函数工具和已启用的任务转移名称冲突。SDK 仍会在原始服务上调用原始 MCP 工具名称。

## 各传输方式的通用模式

选择传输方式后，大多数集成都需要做出相同的后续决策：

- 如何只公开部分工具（[工具过滤](#tool-filtering)）。
- 服务是否还提供可复用的提示词（[提示词](#prompts)）。
- 是否应缓存 `list_tools()`（[缓存](#caching)）。
- MCP 活动如何出现在追踪中（[追踪](#tracing)）。

对于本地 MCP 服务（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`），审批策略和每次调用的 `_meta` 负载也是通用概念。Streamable HTTP 一节展示了最完整的示例，相同模式也适用于其他本地传输方式。

## 1. 托管 MCP 服务工具

托管工具会将整个工具往返过程推送到 OpenAI 的基础设施中。你的代码不再列出和调用工具，而是由
[`HostedMCPTool`][agents.tool.HostedMCPTool] 将服务标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远程服务的工具并调用它们，而无需额外回调你的 Python 进程。托管工具目前适用于支持 Responses API 托管 MCP 集成的 OpenAI 模型。

### 基础托管 MCP 工具

通过将 [`HostedMCPTool`][agents.tool.HostedMCPTool] 添加到智能体的 `tools` 列表来创建托管工具。`tool_config`
字典与发送到 REST API 的 JSON 保持一致：

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

托管服务会自动公开其工具；你无需将其添加到 `mcp_servers`。

如果希望托管工具搜索延迟加载托管 MCP 服务，请设置 `tool_config["defer_loading"] = True` 并将 [`ToolSearchTool`][agents.tool.ToolSearchTool] 添加到智能体。此功能仅在 OpenAI Responses 模型上受支持。有关完整的工具搜索设置和约束，请参阅[工具](tools.md#hosted-tool-search)。

### 流式托管 MCP 结果

托管工具支持流式传输结果，方式与函数工具完全相同。使用 `Runner.run_streamed` 在模型仍在工作时消费增量 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选审批流程

如果某个服务可以执行敏感操作，你可以要求在每次工具执行前进行人工或程序化审批。在 `tool_config` 中配置
`require_approval`，可以使用单一策略（`"always"`、`"never"`），也可以使用将工具名称映射到策略的字典。若要在 Python 中做出决策，请提供 `on_approval_request` 回调。

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

### 连接器支持的托管服务

托管 MCP 也支持 OpenAI 连接器。无需指定 `server_url`，而是提供 `connector_id` 和访问令牌。Responses API 会处理身份验证，托管服务会公开连接器的工具。

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

完整可运行的托管工具示例——包括流式传输、审批和连接器——位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP 服务

当你希望自行管理网络连接时，请使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你控制传输方式，或希望在自己的基础设施内运行服务并保持低延迟时，Streamable HTTP 服务非常适合。

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

构造函数接受其他选项：

- `client_session_timeout_seconds` 控制 HTTP 读取超时。
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而不是文本输出。
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 和 `call_tool()` 添加自动重试。
- `tool_filter` 让你只公开部分工具（参见[工具过滤](#tool-filtering)）。
- `require_approval` 在本地 MCP 工具上启用人在回路中的审批策略。
- `failure_error_function` 自定义模型可见的 MCP 工具失败消息；将其设置为 `None` 则改为抛出错误。
- `tool_meta_resolver` 在 `call_tool()` 之前注入每次调用的 MCP `_meta` 负载。

### 本地 MCP 服务的审批策略

`MCPServerStdio`、`MCPServerSse` 和 `MCPServerStreamableHttp` 都接受 `require_approval`。

支持的形式：

- 对所有工具使用 `"always"` 或 `"never"`。
- `True` / `False`（等同于 always/never）。
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

有关完整的暂停/恢复流程，请参阅[人在回路中](human_in_the_loop.md)以及 `examples/mcp/get_all_mcp_tools_example/main.py`。

### 使用 `tool_meta_resolver` 的每次调用元数据

当你的 MCP 服务期望在 `_meta` 中接收请求元数据（例如租户 ID 或追踪上下文）时，请使用 `tool_meta_resolver`。下面的示例假设你将 `dict` 作为 `context` 传给 `Runner.run(...)`。

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

如果你的运行上下文是 Pydantic 模型、dataclass 或自定义类，请改用属性访问来读取租户 ID。

### MCP 工具输出：文本和图像

当 MCP 工具返回图像内容时，SDK 会自动将其映射为图像工具输出条目。混合文本/图像响应会作为输出项列表转发，因此智能体可以像消费常规函数工具的图像输出一样消费 MCP 图像结果。

## 3. 带 SSE 的 HTTP MCP 服务

!!! warning

    MCP 项目已弃用 Server-Sent Events 传输。对于新的集成，请优先使用 Streamable HTTP 或 stdio，并仅为旧版服务保留 SSE。

如果 MCP 服务实现了带 SSE 的 HTTP 传输，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与 Streamable HTTP 服务相同。

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

## 4. stdio MCP 服务

对于作为本地子进程运行的 MCP 服务，请使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会生成该
进程、保持管道打开，并在上下文管理器退出时自动关闭它们。此选项有助于快速概念验证，或当服务只公开命令行入口点时使用。

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

当你有多个 MCP 服务时，使用 `MCPServerManager` 预先连接它们，并将已连接的子集公开给你的智能体。
有关构造函数选项和重连行为，请参阅 [MCPServerManager API 参考](ref/mcp/manager.md)。

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

- 当 `drop_failed_servers=True`（默认值）时，`active_servers` 仅包含成功连接的服务。
- 失败会记录在 `failed_servers` 和 `errors` 中。
- 设置 `strict=True` 以在第一次连接失败时抛出异常。
- 调用 `reconnect(failed_only=True)` 重试失败的服务，或调用 `reconnect(failed_only=False)` 重启所有服务。
- 使用 `connect_timeout_seconds`、`cleanup_timeout_seconds` 和 `connect_in_parallel` 调优生命周期行为。

## 常见服务能力

以下各节适用于各种 MCP 服务传输方式（具体 API 表面取决于服务类）。

## 工具过滤

每个 MCP 服务都支持工具过滤器，因此你可以只公开智能体所需的函数。过滤可以在构造时发生，也可以按运行动态发生。

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

当同时提供 `allowed_tool_names` 和 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除所有被阻止的工具。

### 动态工具过滤

对于更复杂的逻辑，请传入一个接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 的可调用对象。该可调用对象可以是同步或异步的，并在应公开该工具时返回 `True`。

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

过滤器上下文会公开活动的 `run_context`、请求工具的 `agent`，以及 `server_name`。

## 提示词

MCP 服务还可以提供动态生成智能体 instructions 的提示词。支持提示词的服务会公开两个方法：

- `list_prompts()` 枚举可用的提示词模板。
- `get_prompt(name, arguments)` 获取一个具体提示词，可选择带参数。

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

每次智能体运行都会在每个 MCP 服务上调用 `list_tools()`。远程服务可能带来明显延迟，因此所有 MCP
服务类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时，才将其设置为 `True`。若要稍后强制获取新列表，请在服务实例上调用 `invalidate_tools_cache()`。

## 追踪

[追踪](./tracing.md)会自动捕获 MCP 活动，包括：

1. 对 MCP 服务的列出工具调用。
2. 工具调用中的 MCP 相关信息。

![MCP 追踪截图](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范和设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 和 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的托管 MCP 演示，包括审批和连接器。