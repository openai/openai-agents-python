---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用如何向语言模型暴露工具和上下文。来自官方文档：

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK 支持多种 MCP 传输方式。这样你可以复用现有的 MCP 服务或自行构建，以向智能体暴露基于文件系统、HTTP 或连接器的工具。

## 选择 MCP 集成

在将 MCP 服务接入智能体之前，先决定工具调用应在何处执行，以及可达的传输方式。下表总结了 Python SDK 支持的选项。

| 你的需求                                                                            | 推荐选项                                             |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| 让 OpenAI 的 Responses API 代表模型调用可公开访问的 MCP 服务                         | **托管 MCP 服务工具**，通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| 连接你本地或远程运行的可流式传输的 HTTP 服务                                         | **Streamable HTTP MCP 服务**，通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| 与实现了基于 HTTP 的 Server-Sent Events 的服务通信                                   | **HTTP with SSE MCP 服务**，通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| 启动本地进程并通过 stdin/stdout 通信                                                 | **stdio MCP 服务**，通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下各节将逐一介绍每种选项、如何配置，以及何时优先选择某种传输方式。

## 1. 托管 MCP 服务工具

托管工具将整个工具调用往返流程托管在 OpenAI 的基础设施内。你的代码无需列举与调用工具，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会将服务标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远程服务的工具并直接调用，无需额外回调到你的 Python 进程。托管工具目前可用于支持 Responses API 的托管 MCP 集成的 OpenAI 模型。

### 基础托管 MCP 工具

在智能体的 `tools` 列表中添加一个 [`HostedMCPTool`][agents.tool.HostedMCPTool] 即可创建托管工具。`tool_config` 字典与发送到 REST API 的 JSON 一致：

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

托管工具以与工具调用完全相同的方式支持流式传输。将 `stream=True` 传给 `Runner.run_streamed`，即可在模型仍在处理时消费增量 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选的审批流程

如果某个服务可以执行敏感操作，你可以在每次工具执行前要求人工或程序化审批。在 `tool_config` 中配置 `require_approval`，可传入单一策略（`"always"`、`"never"`）或按工具名映射到策略的字典。若要在 Python 内做出决策，提供一个 `on_approval_request` 回调。

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

该回调可为同步或异步；每当模型需要审批数据以继续运行时都会被调用。

### 基于连接器的托管服务

托管 MCP 也支持 OpenAI 连接器。无需指定 `server_url`，改为提供 `connector_id` 和访问令牌。Responses API 将处理认证，并由托管服务暴露连接器的工具。

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

完整可运行的托管工具示例（包含流式传输、审批与连接器）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP 服务

当你希望自行管理网络连接时，使用 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你可控传输层，或希望在自有基础设施内运行服务同时保持低延迟时，可流式传输的 HTTP 服务是理想选择。

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

构造函数支持以下附加选项：

- `client_session_timeout_seconds` 控制 HTTP 读取超时。
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 与 `retry_backoff_seconds_base` 为 `list_tools()` 与 `call_tool()` 增加自动重试。
- `tool_filter` 允许仅暴露工具子集（见[工具过滤](#tool-filtering)）。

## 3. HTTP with SSE MCP 服务

如果 MCP 服务实现了 HTTP with SSE 传输方式，实例化 [`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与 Streamable HTTP 服务一致。

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

对于作为本地子进程运行的 MCP 服务，使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动进程、保持管道打开，并在上下文管理器退出时自动关闭。该选项适用于快速概念验证或服务仅以命令行入口暴露的场景。

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

## 工具过滤

每个 MCP 服务均支持工具过滤，以便你仅暴露智能体所需的功能。过滤可在构建时设置，或按运行时动态指定。

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

当同时提供 `allowed_tool_names` 与 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除任何被阻止的工具。

### 动态工具过滤

若需更复杂的逻辑，传入一个可调用对象，该对象接收一个 [`ToolFilterContext`][agents.mcp.ToolFilterContext]。该可调用对象可为同步或异步，并在应暴露该工具时返回 `True`。

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

过滤上下文会暴露当前的 `run_context`、请求工具的 `agent`，以及 `server_name`。

## 提示词

MCP 服务还可以提供动态生成智能体指令的提示词。支持提示词的服务将暴露两个方法：

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

每次智能体运行都会对每个 MCP 服务调用 `list_tools()`。远程服务可能引入明显延迟，因此所有 MCP 服务类都提供 `cache_tools_list` 选项。仅在你确信工具定义不会频繁变化时将其设为 `True`。若之后需要强制刷新，调用服务实例的 `invalidate_tools_cache()`。

## 追踪

[追踪](./tracing.md)会自动捕获 MCP 活动，包括：

1. 调用 MCP 服务以列出工具。
2. 工具调用中的 MCP 相关信息。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与 Streamable HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的托管 MCP 演示，包含审批与连接器。