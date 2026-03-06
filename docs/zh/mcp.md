---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用向语言模型暴露工具和上下文的方式。来自官方文档：

> MCP 是一种开放协议，用于标准化应用如何向 LLM 提供上下文。可以将 MCP 视为 AI 应用的 USB-C 端口。正如 USB-C 提供了一种将设备连接到各种外设和配件的标准化方式，MCP 也提供了一种将 AI 模型连接到不同数据源和工具的标准化方式。

Agents Python SDK 支持多种 MCP 传输方式。这使你可以复用现有的 MCP 服务，或构建自己的服务，以向智能体暴露基于文件系统、HTTP 或连接器的工具。

## MCP 集成选择

在将 MCP 服务接入智能体之前，请先确定工具调用应在何处执行，以及你可访问哪些传输方式。下表汇总了 Python SDK 支持的选项。

| 你的需求 | 推荐选项 |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用一个可公开访问的 MCP 服务| 通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 使用**托管 MCP 服务工具** |
| 连接你在本地或远程运行的 Streamable HTTP 服务 | 通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 使用**Streamable HTTP MCP 服务** |
| 与实现了带 Server-Sent Events 的 HTTP 的服务通信 | 通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 使用**带 SSE 的 HTTP MCP 服务** |
| 启动本地进程并通过 stdin/stdout 通信 | 通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 使用**stdio MCP 服务** |

下文将逐一介绍每个选项、如何配置，以及何时优先选择某种传输方式。

## 智能体级 MCP 配置

除选择传输方式外，你还可以通过设置 `Agent.mcp_config` 来调整 MCP 工具的准备方式。

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

- `convert_schemas_to_strict` 为尽力而为。如果某个 schema 无法转换，则使用原始 schema。
- `failure_error_function` 控制如何将 MCP 工具调用失败信息暴露给模型。
- 当未设置 `failure_error_function` 时，SDK 使用默认的工具错误格式化器。
- 服务级 `failure_error_function` 会覆盖该服务的 `Agent.mcp_config["failure_error_function"]`。

## 跨传输方式的通用模式

选择传输方式后，大多数集成都需要做出相同的后续决策：

- 如何仅暴露部分工具（[工具过滤](#tool-filtering)）。
- 服务是否也提供可复用的提示词（[Prompts](#prompts)）。
- 是否应缓存 `list_tools()`（[缓存](#caching)）。
- MCP 活动如何显示在追踪中（[追踪](#tracing)）。

对于本地 MCP 服务（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`），审批策略和每次调用的 `_meta` 负载也是通用概念。Streamable HTTP 部分展示了最完整的示例，同样的模式也适用于其他本地传输方式。

## 1. 托管 MCP 服务工具

托管工具将整个工具往返流程交由 OpenAI 基础设施处理。你的代码无需列出和调用工具，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会将服务标签（及可选连接器元数据）转发给 Responses API。模型会列出远程服务的工具并调用它们，而无需额外回调你的 Python 进程。托管工具目前适用于支持 Responses API 托管 MCP 集成的 OpenAI 模型。

### 基础托管 MCP 工具

通过将 [`HostedMCPTool`][agents.tool.HostedMCPTool] 添加到智能体的 `tools` 列表来创建托管工具。`tool_config` 字典与发送到 REST API 的 JSON 对应：

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

托管服务会自动暴露其工具；你无需将其添加到 `mcp_servers`。

### 托管 MCP 结果的流式传输

托管工具以与工具调用完全相同的方式支持流式结果。使用 `Runner.run_streamed` 可在模型仍在处理时消费增量 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选审批流程

如果服务可执行敏感操作，你可以要求在每次工具执行前进行人工或程序化审批。在 `tool_config` 中配置 `require_approval`，可使用单一策略（`"always"`、`"never"`）或将工具名映射到策略的字典。若要在 Python 内做决策，请提供 `on_approval_request` 回调。

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

该回调可为同步或异步；当模型需要审批数据以继续运行时会被调用。

### 基于连接器的托管服务

托管 MCP 也支持 OpenAI 连接器。无需指定 `server_url`，改为提供 `connector_id` 和访问令牌。Responses API 会处理认证，托管服务会暴露该连接器的工具。

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

完整可运行的托管工具示例（包括流式传输、审批与连接器）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP 服务

当你希望自行管理网络连接时，请使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你控制传输层，或希望在自有基础设施中运行服务并保持低延迟时，Streamable HTTP 服务是理想选择。

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

构造函数接受以下附加选项：

- `client_session_timeout_seconds` 控制 HTTP 读取超时。
- `use_structured_content` 控制是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 与 `call_tool()` 增加自动重试。
- `tool_filter` 允许你仅暴露部分工具（见[工具过滤](#tool-filtering)）。
- `require_approval` 在本地 MCP 工具上启用人类参与审批策略。
- `failure_error_function` 自定义模型可见的 MCP 工具失败消息；将其设为 `None` 则改为抛出错误。
- `tool_meta_resolver` 在 `call_tool()` 之前注入每次调用的 MCP `_meta` 负载。

### 本地 MCP 服务的审批策略

`MCPServerStdio`、`MCPServerSse` 和 `MCPServerStreamableHttp` 都接受 `require_approval`。

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

完整的暂停/恢复流程，请参见[Human-in-the-loop](human_in_the_loop.md) 和 `examples/mcp/get_all_mcp_tools_example/main.py`。

### 使用 `tool_meta_resolver` 提供每次调用元数据

当你的 MCP 服务期望在 `_meta` 中接收请求元数据（例如租户 ID 或追踪上下文）时，请使用 `tool_meta_resolver`。以下示例假设你将 `dict` 作为 `context` 传入 `Runner.run(...)`。

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

如果你的运行上下文是 Pydantic 模型、dataclass 或自定义类，请改用属性访问读取租户 ID。

### MCP 工具输出：文本与图像

当 MCP 工具返回图像内容时，SDK 会自动将其映射为图像工具输出条目。文本/图像混合响应会作为输出项列表转发，因此智能体可像消费常规工具调用的图像输出一样消费 MCP 图像结果。

## 3. 带 SSE 的 HTTP MCP 服务

!!! warning

    MCP 项目已弃用 Server-Sent Events 传输。新集成请优先使用 Streamable HTTP 或 stdio，仅在遗留服务中保留 SSE。

如果 MCP 服务实现了带 SSE 的 HTTP 传输，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与 Streamable HTTP 服务完全一致。

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

对于以本地子进程运行的 MCP 服务，请使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动该进程、保持管道打开，并在上下文管理器退出时自动关闭。该选项适合快速概念验证，或服务仅暴露命令行入口的场景。

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

当你有多个 MCP 服务时，请使用 `MCPServerManager` 提前连接它们，并将已连接子集暴露给智能体。
构造参数和重连行为请参阅 [MCPServerManager API 参考](ref/mcp/manager.md)。

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

- 当 `drop_failed_servers=True`（默认）时，`active_servers` 仅包含成功连接的服务。
- 失败会记录在 `failed_servers` 和 `errors` 中。
- 设置 `strict=True` 可在首次连接失败时抛出异常。
- 调用 `reconnect(failed_only=True)` 可重试失败服务，或调用 `reconnect(failed_only=False)` 以重启所有服务。
- 使用 `connect_timeout_seconds`、`cleanup_timeout_seconds` 和 `connect_in_parallel` 调整生命周期行为。

## 常见服务能力

下文适用于各类 MCP 服务传输（具体 API 取决于服务类）。

## 工具过滤

每个 MCP 服务都支持工具过滤，使你仅暴露智能体所需功能。过滤可在构造时进行，也可在每次运行时动态进行。

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

当同时提供 `allowed_tool_names` 与 `blocked_tool_names` 时，SDK 会先应用允许列表，再从剩余集合中移除被阻止工具。

### 动态工具过滤

对于更复杂逻辑，可传入一个接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 的可调用对象。该可调用对象可为同步或异步，并在工具应被暴露时返回 `True`。

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

过滤上下文暴露当前 `run_context`、请求工具的 `agent` 以及 `server_name`。

## Prompts

MCP 服务还可提供动态生成智能体指令的提示词。支持提示词的服务会暴露两个方法：

- `list_prompts()` 枚举可用的提示词模板。
- `get_prompt(name, arguments)` 获取具体提示词，可选传入参数。

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

每次智能体运行都会在每个 MCP 服务上调用 `list_tools()`。远程服务可能引入明显延迟，因此所有 MCP 服务类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时才将其设为 `True`。若后续要强制获取最新列表，请在服务实例上调用 `invalidate_tools_cache()`。

## 追踪

[追踪](./tracing.md)会自动捕获 MCP 活动，包括：

1. 调用 MCP 服务以列出工具。
2. 工具调用中的 MCP 相关信息。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的托管 MCP 演示，包括审批与连接器。