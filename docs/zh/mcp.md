---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）标准化了应用如何向语言模型暴露工具和上下文。摘自官方文档：

> MCP 是一种开放协议，用于标准化应用向 LLM 提供上下文的方式。可以把 MCP 想象成 AI 应用的 USB-C 接口。就像 USB-C 提供了一种标准化方式，将你的设备连接到各种外设和配件，MCP 也提供了一种标准化方式，将 AI 模型连接到不同的数据源和工具。

Agents Python SDK 支持多种 MCP 传输方式。这使你可以复用现有 MCP 服务或自行构建，以向智能体暴露文件系统、HTTP 或由连接器支持的工具。

## 选择 MCP 集成方式

在将 MCP 服务接入智能体前，请先决定工具调用应在哪里执行，以及你可以使用哪些传输方式。下表总结了 Python SDK 支持的选项。

| 你的需求                                                                                 | 推荐选项                                               |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 让 OpenAI 的 Responses API 代表模型调用可公网访问的 MCP 服务                              | **托管的 MCP 服务器工具**，通过 [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| 连接你在本地或远程运行的 Streamable HTTP 服务                                             | **可流式 HTTP MCP 服务器**，通过 [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| 与实现了带 Server-Sent Events 的 HTTP 的服务器通信                                        | **HTTP + SSE MCP 服务器**，通过 [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| 启动本地进程并通过 stdin/stdout 通信                                                      | **stdio MCP 服务器**，通过 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

下文将逐一介绍每种选项、配置方法，以及何时优先选择某种传输方式。

## 1. 托管的 MCP 服务器工具

托管工具将整个工具的往返调用交给 OpenAI 的基础设施处理。你的代码无需列出和调用工具，[`HostedMCPTool`][agents.tool.HostedMCPTool] 会将服务器标签（以及可选的连接器元数据）转发给 Responses API。模型会列出远程服务器的工具并直接调用，无需额外回调到你的 Python 进程。托管工具目前适用于支持 Responses API 托管 MCP 集成的 OpenAI 模型。

### 基础托管 MCP 工具

通过在智能体的 `tools` 列表中添加 [`HostedMCPTool`][agents.tool.HostedMCPTool] 来创建托管工具。`tool_config` 字典与您发送到 REST API 的 JSON 相同：

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

托管服务器会自动暴露其工具；你无需将其添加到 `mcp_servers`。

### 托管 MCP 结果的流式传输

托管工具以与工具调用相同的方式支持流式传输。向 `Runner.run_streamed` 传入 `stream=True`，以在模型运行期间消费增量的 MCP 输出：

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 可选的审批流程

如果服务器可以执行敏感操作，你可以在每次工具执行前要求人工或程序化审批。在 `tool_config` 中配置 `require_approval`，可以是单一策略（`"always"`、`"never"`）或一个将工具名映射到策略的字典。若要在 Python 内部做决定，提供一个 `on_approval_request` 回调。

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

该回调可以是同步或异步的，当模型需要审批数据以继续运行时会被调用。

### 由连接器支持的托管服务器

托管 MCP 也支持 OpenAI 连接器。你可以不指定 `server_url`，改为提供 `connector_id` 和访问令牌。Responses API 会处理认证，托管服务器将暴露该连接器的工具。

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

包含流式传输、审批和连接器的完整托管工具示例位于
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)。

## 2. 可流式 HTTP MCP 服务器

当你希望自行管理网络连接时，请使用
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp]。当你可控传输层，或想把服务器运行在自己的基础设施中以保持低延迟时，可流式 HTTP 服务器是理想选择。

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
- `max_retry_attempts` 和 `retry_backoff_seconds_base` 为 `list_tools()` 和 `call_tool()` 添加自动重试。
- `tool_filter` 允许仅暴露工具的子集（参见[工具过滤](#tool-filtering)）。

## 3. HTTP + SSE MCP 服务器

如果 MCP 服务器实现了带 SSE 的 HTTP 传输，请实例化
[`MCPServerSse`][agents.mcp.server.MCPServerSse]。除了传输层不同，其 API 与可流式 HTTP 服务器完全相同。

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

## 4. stdio MCP 服务器

对于以本地子进程方式运行的 MCP 服务器，请使用 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]。SDK 会启动进程、保持管道打开，并在上下文管理器退出时自动关闭。这一选项有助于快速原型验证，或当服务器仅以命令行入口形式暴露时使用。

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

每个 MCP 服务器都支持工具过滤，以便你仅暴露智能体所需的功能。过滤可以在构造时进行，也可以在每次运行时动态设置。

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

当同时提供 `allowed_tool_names` 和 `blocked_tool_names` 时，SDK 会先应用允许列表，然后从剩余集合中移除任何被阻止的工具。

### 动态工具过滤

对于更复杂的逻辑，传入一个可调用对象，该对象接收 [`ToolFilterContext`][agents.mcp.ToolFilterContext]。该可调用对象可以是同步或异步的，并在工具应被暴露时返回 `True`。

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

MCP 服务器还可以提供提示词，以动态生成智能体指令。支持提示词的服务器会暴露两个方法：

- `list_prompts()` 枚举可用的提示词模板。
- `get_prompt(name, arguments)` 获取具体提示词，可选带参数。

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

每次智能体运行都会对每个 MCP 服务器调用 `list_tools()`。远程服务器可能引入明显的延迟，因此所有 MCP 服务器类都暴露了 `cache_tools_list` 选项。仅当你确信工具定义不经常变化时，才将其设为 `True`。如需之后强制刷新工具列表，请在服务器实例上调用 `invalidate_tools_cache()`。

## 追踪

[Tracing](./tracing.md) 会自动捕获 MCP 活动，包括：

1. 对 MCP 服务器的工具列表调用。
2. 工具调用中的 MCP 相关信息。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 延伸阅读

- [Model Context Protocol](https://modelcontextprotocol.io/) – 规范与设计指南。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 可运行的 stdio、SSE 与可流式 HTTP 样例。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 完整的托管 MCP 演示，包括审批和连接器。