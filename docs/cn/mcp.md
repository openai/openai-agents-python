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

hosted サーバーはそのツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

### ストリーミング対応の hosted MCP 実行結果

Hosted ツールは 関数ツール とまったく同じ方法で ストリーミング に対応します。`Runner.run_streamed` に `stream=True` を渡すと、モデルがまだ動作中でも MCP の出力を増分で取り込めます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### オプションの承認フロー

サーバーが機密性の高い操作を行える場合、各ツール実行前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を、単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict で設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

コールバックは同期・非同期のどちらでもよく、モデルが実行を続けるために承認データを必要とするたびに呼び出されます。

### コネクタ対応の hosted サーバー

Hosted MCP は OpenAI connectors にも対応します。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、hosted サーバーがそのコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む、完全に動作する hosted ツールのサンプルは
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、レイテンシを低く保ちながら自分のインフラ内でサーバーを実行したい場合に最適です。

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

コンストラクタは追加のオプションを受け付けます:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力よりも `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` は、公開するツールをサブセットに限定できます（[Tool filtering](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP servers

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

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

ローカルのサブプロセスとして実行する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK がプロセスを起動し、パイプを開いたまま維持し、コンテキストマネージャの終了時に自動的に閉じます。このオプションは、迅速なプロトタイピングや、サーバーがコマンドラインのエントリポイントのみを公開している場合に役立ちます。

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

## ツールのフィルタリング

各 MCP サーバーはツールフィルタに対応しており、エージェント が必要とする関数だけを公開できます。フィルタリングは構築時にも、実行ごとに動的にも行えます。

### 静的なツールフィルタリング

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、単純な許可/ブロックリストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK は最初に許可リストを適用し、その後残りのセットからブロックされたツールを削除します。

### 動的なツールフィルタリング

より高度なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出し可能オブジェクトは同期・非同期のどちらでもよく、ツールを公開すべきときに `True` を返します。

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

フィルタコンテキストは、アクティブな `run_context`、ツールを要求している `agent`、そして `server_name` を公開します。

## プロンプト

MCP サーバーは、エージェントの instructions を動的に生成するプロンプトも提供できます。プロンプトに対応するサーバーは次の 2 つのメソッドを公開します:

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じて パラメーター 付きの具体的なプロンプトを取得します。

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

## キャッシュ

すべての エージェント 実行は各 MCP サーバーに対して `list_tools()` を呼び出します。リモートサーバーは顕著なレイテンシを招く可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開します。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。後で新しいリストを強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[Tracing](./tracing.md) は MCP のアクティビティを自動的に捕捉します。含まれるもの:

1. ツールを列挙するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 参考情報

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む、完全な hosted MCP のデモ。