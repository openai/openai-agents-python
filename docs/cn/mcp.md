---
search:
  exclude: true
---
# 模型上下文协议 (MCP)

[模型上下文协议](https://modelcontextprotocol.io/introduction) (MCP) 标准化了应用程序如何向语言模型公开工具和上下文的方法。来自官方文档：

> MCP是一个开放协议，它标准化了应用程序如何向LLM提供上下文的方法。将MCP视为AI应用的USB-C端口。正如USB-C提供了一种将设备连接到各种外围设备和配件的标准化方式，MCP提供了一种将AI模型连接到不同数据源和工具的标准化方式。

Agents Python SDK支持多种MCP传输方式。这使你可以重用现有的MCP服务器或构建自己的服务器，将文件系统、HTTP或由连接器支持的工具暴露给智能体。

## 选择MCP集成

在将MCP服务器连接到智能体之前，决定工具调用应该在哪里执行以及你可以访问哪些传输方式。下表总结了Python SDK支持的选项。

| 你需要什么                                                                                | 推荐选项                                                  |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| 让OpenAI的Responses API代表模型调用公开可访问的MCP服务器                               | **托管MCP服务器工具**（通过[`HostedMCPTool`][agents.tool.HostedMCPTool]） |
| 连接到你在本地或远程运行的可流式HTTP服务器                                            | **可流式HTTP MCP服务器**（通过[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]） |
| 与实现了带服务器发送事件(SSE)的HTTP服务器进行交互                                       | **带SSE的HTTP MCP服务器**（通过[`MCPServerSse`][agents.mcp.server.MCPServerSse]） |
| 启动本地进程并通过stdin/stdout进行通信                                                 | **stdio MCP服务器**（通过[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]） |

以下各节将介绍每个选项、如何配置它们，以及何时优先选择某个传输方式。

## 1. 托管MCP服务器工具

托管工具将整个工具往返过程推送到OpenAI的基础设施中。你的代码不再列出和调用工具，而是[`HostedMCPTool`][agents.tool.HostedMCPTool]将服务器标签（和可选的连接器元数据）转发到Responses API。模型列出远程服务器的工具并在没有额外回调到你的Python进程的情况下调用它们。托管工具目前适用于支持Responses API的托管MCP集成的OpenAI模型。

### 基本托管MCP工具

通过向智能体的`tools`列表添加[`HostedMCPTool`][agents.tool.HostedMCPTool]来创建托管工具。`tool_config`字典反映了你要发送到REST API的JSON：

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

托管服务器会自动公开其工具，无需将其添加到 `mcp_servers`。

### 支持流式传输的托管MCP执行结果

托管工具以与函数工具完全相同的方式支持流式传输。向 `Runner.run_streamed` 传递 `stream=True`，即可在模型仍在运行时增量获取MCP输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选的审批流程

如果服务器能够执行敏感操作，可以在每次工具执行前要求人工或程序审批。将 `tool_config` 中的 `require_approval` 设置为单一策略（`"always"`、`"never"`）或从工具名到策略的字典。要在Python中进行判断，请指定 `on_approval_request` 回调函数。

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

回调可以是同步或异步的，每当模型需要审批数据才能继续执行时就会被调用。

### 支持连接器的托管服务器

托管MCP也支持OpenAI连接器。不指定 `server_url`，而是指定 `connector_id` 和访问令牌。Responses API会处理认证，托管服务器会公开该连接器的工具。

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

包含流式传输、审批和连接器的完整可运行托管工具示例可在
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) 中找到。

## 2. 可流式HTTP MCP服务器

如果你想自己管理网络连接，可以使用 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。可流式HTTP服务器最适合需要自己控制传输方式的场景，或者在自己的基础设施中运行服务器同时保持低延迟的情况。

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

构造函数接受额外的选项：

- `client_session_timeout_seconds` 控制HTTP读取超时时间。
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 和 `call_tool()` 添加自动重试。
- `tool_filter` 可以将公开的工具限制为子集（参见[工具过滤](#工具过滤)）。

## 3. 带SSE的HTTP MCP服务器

如果MCP服务器实现了带SSE（服务器发送事件）的HTTP传输，可以实例化 [`MCPServerSse`][agents.mcp.server.MCPServerSse]。除了传输方式外，API与可流式HTTP服务器完全相同。

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

## 4. stdio MCP服务器

对于作为本地子进程运行的MCP服务器，使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK会启动进程，保持管道打开，并在上下文管理器退出时自动关闭。此选项适用于快速原型开发，或者当服务器仅公开命令行入口点时。

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

每个MCP服务器都支持工具过滤器，可以只公开智能体需要的函数。过滤可以在构建时进行，也可以在每次运行时动态进行。

### 静态工具过滤

使用 [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] 设置简单的允许/阻止列表：

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

如果同时指定了 `allowed_tool_names` 和 `blocked_tool_names`，SDK会首先应用允许列表，然后从剩余集合中删除被阻止的工具。

### 动态工具过滤

对于更高级的逻辑，可以传递一个接受 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 的可调用对象。可调用对象可以是同步或异步的，当应该公开工具时返回 `True`。

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

过滤器上下文公开了活动的 `run_context`、请求工具的 `agent` 以及 `server_name`。

## 提示词

MCP服务器还可以提供提示词来动态生成智能体的指令。支持提示词的服务器会公开以下两个方法：

- `list_prompts()` 枚举可用的提示词模板。
- `get_prompt(name, arguments)` 获取具体的提示词，必要时可带参数。

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

所有智能体执行都会对每个MCP服务器调用 `list_tools()`。由于远程服务器可能导致显著的延迟，所有MCP服务器类都公开了 `cache_tools_list` 选项。只有当你确信工具定义不会频繁更改时才设置为 `True`。如需在之后强制获取新列表，可以在服务器实例上调用 `invalidate_tools_cache()`。

## 追踪

[追踪](./tracing.md)会自动捕获MCP活动，包括：

1. 为枚举工具而对MCP服务器的调用。
2. 与工具调用相关的MCP信息。

![MCP追踪截图](../assets/images/mcp-tracing.jpg)

## 参考资料

- [模型上下文协议](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的stdio、SSE、可流式HTTP示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 包含审批和连接器的完整托管MCP演示。
