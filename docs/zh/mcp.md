---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用如何向语言模型暴露工具与上下文。官方文档描述如下：

> MCP 是一种开放协议，用于标准化应用向 LLM 提供上下文的方式。可以把 MCP 看作 AI 应用的 USB‑C 接口。正如 USB‑C 为设备连接各类外设与配件提供了标准化方式，MCP 为 AI 模型连接不同的数据源与工具提供了标准化方式。

Agents Python SDK 支持多种 MCP 传输方式。这让你可以复用现有 MCP 服务，或自行构建服务，将文件系统、HTTP 或基于连接器的工具暴露给智能体。

## Choosing an MCP integration

在将 MCP 服务接入智能体之前，请先决定工具调用应在何处执行，以及可用哪些传输方式。下表总结了 Python SDK 支持的选项。

| What you need                                                                        | Recommended option                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Let OpenAI's Responses API call a publicly reachable MCP server on the model's behalf| **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| Connect to Streamable HTTP servers that you run locally or remotely                  | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Talk to servers that implement HTTP with Server-Sent Events                          | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| Launch a local process and communicate over stdin/stdout                             | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下小节将逐一介绍各选项、配置方法以及何时优先选择某种传输方式。

## 1. Hosted MCP server tools

Hosted 工具将完整的工具调用往返置于 OpenAI 的基础设施内。你的代码无需列出并调用工具，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会把服务器标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远程服务器的工具并直接调用，无需回调你的 Python 进程。当前 Hosted 工具可用于支持 Responses API 的 hosted MCP 集成的 OpenAI 模型。

### Basic hosted MCP tool

通过在智能体的 `tools` 列表中加入 [`HostedMCPTool`][agents.tool.HostedMCPTool] 来创建一个 hosted 工具。`tool_config` 字典与发送至 REST API 的 JSON 对应：

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

Hosted 服务器会自动暴露其工具；你无需将其添加到 `mcp_servers`。

### Streaming hosted MCP results

Hosted 工具与工具调用的流式传输方式完全一致。向 `Runner.run_streamed` 传入 `stream=True`，即可在模型仍在处理时消费增量的 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

若服务器可执行敏感操作，你可以在每次工具执行前要求人工或程序化审批。在 `tool_config` 中通过 `require_approval` 配置单一策略（`"always"`、`"never"`）或将工具名映射到策略的字典。若要在 Python 内部做出决策，提供一个 `on_approval_request` 回调。

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

回调可为同步或异步；每当模型需要审批数据以继续运行时都会被调用。

### Connector-backed hosted servers

Hosted MCP 也支持 OpenAI 连接器。你可以不指定 `server_url`，改为提供 `connector_id` 与访问令牌。Responses API 会处理认证，Hosted 服务器将暴露该连接器的工具。

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

完整可运行的 hosted 工具示例（包含流式传输、审批与连接器）位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. Streamable HTTP MCP servers

当你希望自行管理网络连接时，请使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你控制传输层，或希望在自有基础设施中运行服务器并保持较低延迟时，可流式 HTTP 服务器是理想选择。

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
- `use_structured_content` 切换是否优先使用 `tool_result.structured_content` 而非文本输出。
- `max_retry_attempts` 与 `retry_backoff_seconds_base` 为 `list_tools()` 与 `call_tool()` 添加自动重试。
- `tool_filter` 仅暴露工具子集（见 [工具筛选](#tool-filtering)）。

## 3. HTTP with SSE MCP servers

若 MCP 服务器实现了 HTTP with SSE 传输方式，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除传输方式外，其 API 与可流式 HTTP 服务器相同。

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

对于以本地子进程运行的 MCP 服务器，使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动进程、保持管道打开，并在上下文管理器退出时自动关闭。这一选项适用于快速原型或仅提供命令行入口的服务器。

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

## 工具筛选

每个 MCP 服务器均支持工具筛选，以便仅暴露智能体需要的功能。筛选可在构建时进行，也可按运行动态应用。

### 静态工具筛选

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

当同时提供 `allowed_tool_names` 与 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除任意被阻止的工具。

### 动态工具筛选

对于更复杂的逻辑，传入一个接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext] 的可调用对象。该可调用对象可为同步或异步，返回 `True` 表示应暴露该工具。

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

筛选上下文会暴露当前的 `run_context`、请求工具的 `agent`，以及 `server_name`。

## Prompts

MCP 服务器还可提供用于动态生成智能体 instructions 的 Prompts。支持 Prompts 的服务器会暴露两个方法：

- `list_prompts()` 枚举可用的提示模板。
- `get_prompt(name, arguments)` 获取具体提示，可选带参数。

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

每次智能体运行都会在每个 MCP 服务器上调用 `list_tools()`。远程服务器可能引入显著延迟，因此所有 MCP 服务器类都提供 `cache_tools_list` 选项。仅当你确信工具定义不会频繁变化时才将其设为 `True`。若之后需要强制刷新列表，可在服务器实例上调用 `invalidate_tools_cache()`。

## Tracing

[Tracing](./tracing.md) 会自动捕获 MCP 活动，包括：

1. 向 MCP 服务器发起的工具列表请求。
2. 与工具调用相关的 MCP 信息。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与可流式 HTTP 示例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的 hosted MCP 演示，包括审批与连接器。