---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用如何向语言模型暴露工具与上下文。来自官方文档：

> MCP 是一个开放协议，用于标准化应用如何向 LLMs 提供上下文。可以把 MCP 想象成 AI 应用的 USB-C 端口。正如 USB-C 提供了一种标准化方式，将你的设备连接到各种外设与配件，MCP 也提供了一种标准化方式，将 AI 模型连接到不同的数据源与工具。

Agents Python SDK 支持多种 MCP 传输方式。这让你可以复用现有的 MCP 服务，或自行构建服务，以向智能体暴露由文件系统、HTTP 或连接器支撑的工具。

## MCP 集成选型

在将 MCP 服务接入智能体之前，先确定工具调用应在哪里执行，以及你能够访问哪些传输方式。下表概括了 Python SDK 支持的选项。

| 你的需求 | 推荐选项 |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用一个可公网访问的 MCP 服务 | 通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] 使用 **Hosted MCP server tools** |
| 连接到你在本地或远端运行的 Streamable HTTP 服务 | 通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 使用 **Streamable HTTP MCP servers** |
| 与实现了带 Server-Sent Events 的 HTTP 的服务通信 | 通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] 使用 **HTTP with SSE MCP servers** |
| 启动本地进程并通过 stdin/stdout 通信 | 通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 使用 **stdio MCP servers** |

下面的章节会分别介绍每个选项、如何配置，以及何时应优先选择某种传输方式。

## 智能体级 MCP 配置

除了选择传输方式，你还可以通过设置 `Agent.mcp_config` 来调整 MCP 工具的准备方式。

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

注意事项：

- `convert_schemas_to_strict` 采用尽力而为策略。如果某个 schema 无法转换，则使用原始 schema。
- `failure_error_function` 控制 MCP 工具调用失败如何呈现给模型。
- 未设置 `failure_error_function` 时，SDK 会使用默认的工具错误格式化器。
- 服务级 `failure_error_function` 会覆盖该服务的 `Agent.mcp_config["failure_error_function"]`。

## 1. Hosted MCP server tools

Hosted tools 将完整的工具调用往返都放到 OpenAI 的基础设施中完成。你的代码不再负责列出与调用工具；相反，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会将服务标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远端服务的工具并调用它们，而无需额外回调到你的 Python 进程。Hosted tools 当前适用于支持 Responses API 的 hosted MCP 集成的 OpenAI 模型。

### 基础 hosted MCP 工具

通过将一个 [`HostedMCPTool`][agents.tool.HostedMCPTool] 加入智能体的 `tools` 列表来创建 hosted 工具。`tool_config` 字典与发送到 REST API 的 JSON 保持一致：

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

该 hosted 服务会自动暴露其工具；你不需要将它添加到 `mcp_servers`。

### 流式传输 hosted MCP 结果

Hosted tools 以与工具调用相同的方式支持流式传输结果。向 `Runner.run_streamed` 传入 `stream=True`，即可在模型仍在工作时消费增量的 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选的审批流程

如果某个服务可能执行敏感操作，你可以要求在每次工具执行前进行人工或程序化审批。在 `tool_config` 中配置 `require_approval`，可以是单一策略（`"always"`、`"never"`），也可以是将工具名映射到策略的字典。若要在 Python 中做决策，请提供 `on_approval_request` 回调。

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

该回调可以是同步或异步的，并且当模型需要审批数据以继续运行时就会被调用。

### 由连接器支撑的 hosted 服务

Hosted MCP 也支持 OpenAI connectors。无需指定 `server_url`，改为提供 `connector_id` 与访问令牌。Responses API 会处理认证，而 hosted 服务会暴露连接器的工具。

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

完整可运行的 hosted 工具示例（包括流式传输、审批与连接器）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP servers

当你希望自行管理网络连接时，使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你能控制传输方式，或希望在自有基础设施中运行服务同时保持低延迟时，Streamable HTTP 服务是理想选择。

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

构造函数还接受额外选项：

- `client_session_timeout_seconds` 控制 HTTP 读取超时。
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 与 `retry_backoff_seconds_base` 为 `list_tools()` 与 `call_tool()` 增加自动重试。
- `tool_filter` 允许你只暴露部分工具（见 [工具过滤](#tool-filtering)）。
- `require_approval` 为本地 MCP 工具启用 human-in-the-loop 审批策略。
- `failure_error_function` 自定义对模型可见的 MCP 工具失败消息；将其设为 `None` 则改为抛出错误。
- `tool_meta_resolver` 在 `call_tool()` 前为每次调用注入 MCP `_meta` 载荷。

### 本地 MCP 服务的审批策略

`MCPServerStdio`、`MCPServerSse` 与 `MCPServerStreamableHttp` 都接受 `require_approval`。

支持的形式：

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

如需完整的暂停/恢复流程，参见 [Human-in-the-loop](human_in_the_loop.md) 与 `examples/mcp/get_all_mcp_tools_example/main.py`。

### 使用 `tool_meta_resolver` 的逐次调用元数据

当你的 MCP 服务期望在 `_meta` 中接收请求元数据（例如租户 ID 或追踪上下文）时，使用 `tool_meta_resolver`。下面示例假设你将一个 `dict` 作为 `context` 传给 `Runner.run(...)`。

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

### MCP 工具输出：文本与图片

当 MCP 工具返回图片内容时，SDK 会自动将其映射为图片工具输出条目。混合的文本/图片响应会作为输出条目列表被转发，因此智能体可以用与消费常规工具调用的图片输出相同的方式来消费 MCP 图片结果。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP 项目已弃用 Server-Sent Events 传输方式。新集成请优先使用 Streamable HTTP 或 stdio，仅在遗留服务场景保留 SSE。

如果 MCP 服务实现了 HTTP with SSE 传输方式，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式不同外，其 API 与 Streamable HTTP 服务完全一致。

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

对于作为本地子进程运行的 MCP 服务，使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动该进程，保持管道打开，并在上下文管理器退出时自动关闭它们。该选项适用于快速概念验证，或当服务仅提供命令行入口时。

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
- 失败会记录在 `failed_servers` 与 `errors` 中。
- 设置 `strict=True` 会在首次连接失败时抛出异常。
- 调用 `reconnect(failed_only=True)` 重试失败的服务，或 `reconnect(failed_only=False)` 重启所有服务。
- 使用 `connect_timeout_seconds`、`cleanup_timeout_seconds` 与 `connect_in_parallel` 来调节生命周期行为。

## 工具过滤

每个 MCP 服务都支持工具过滤器，以便你只暴露智能体所需的函数。过滤既可以在构造时完成，也可以在每次运行时动态进行。

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

对于更复杂的逻辑，传入一个可调用对象，它会接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext]。该可调用对象可以是同步或异步的，并在工具应被暴露时返回 `True`。

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

过滤上下文会暴露当前的 `run_context`、请求工具的 `agent` 以及 `server_name`。

## 提示词

MCP 服务也可以提供提示词，用于动态生成智能体指令。支持提示词的服务会暴露两个方法：

- `list_prompts()` 枚举可用的提示词模板。
- `get_prompt(name, arguments)` 获取一个具体提示词，并可选带参数。

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

每次智能体运行都会对每个 MCP 服务调用 `list_tools()`。远端服务可能引入明显延迟，因此所有 MCP 服务类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时才将其设为 `True`。若之后需要强制刷新列表，请在服务实例上调用 `invalidate_tools_cache()`。

## 追踪

[Tracing](./tracing.md) 会自动捕获 MCP 活动，包括：

1. 调用 MCP 服务以列出工具。
2. 工具调用中的 MCP 相关信息。

![MCP 追踪截图](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的 hosted MCP 演示，包括审批与连接器。