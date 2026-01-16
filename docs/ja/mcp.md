---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが ツール とコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントからの引用です:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、独自の サーバー を構築して、ファイルシステム、HTTP、またはコネクタで裏付けられた ツール を エージェント に公開できます。

## Choosing an MCP integration

MCP サーバーを エージェント に接続する前に、ツール呼び出しをどこで実行するか、また到達可能なトランスポートはどれかを決めます。以下のマトリクスは、Python SDK がサポートするオプションをまとめたものです。

| 必要なこと                                                                            | 推奨オプション                                              |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| OpenAI の Responses API に、モデルの代わりに公開到達可能な MCP サーバーを呼び出させたい | **ホスト型 MCP サーバーのツール**（[`HostedMCPTool`][agents.tool.HostedMCPTool] 経由） |
| ローカルまたはリモートで実行する Streamable HTTP サーバーに接続したい                  | **Streamable HTTP MCP サーバー**（[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 経由） |
| Server-Sent Events を実装した サーバー と通信したい                                     | **HTTP with SSE MCP サーバー**（[`MCPServerSse`][agents.mcp.server.MCPServerSse] 経由） |
| ローカルプロセスを起動して stdin/stdout で通信したい                                   | **stdio MCP サーバー**（[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 経由） |

以下のセクションでは、それぞれのオプションの使い方、設定方法、そしてどのトランスポートを選ぶべきかを説明します。

## 1. Hosted MCP server tools

ホスト型 ツール は、ツールの往復処理全体を OpenAI のインフラストラクチャに委譲します。あなたのコードが ツール を列挙・呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] が サーバー のラベル（およびオプションのコネクタメタデータ）を Responses API に転送します。モデルはリモート サーバー の ツール を一覧し、あなたの Python プロセスへの追加のコールバックなしにそれらを呼び出します。ホスト型 ツール は現在、Responses API の hosted MCP 連携に対応した OpenAI モデルで動作します。

### Basic hosted MCP tool

エージェント の `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型 ツール を作成します。`tool_config` の dict は、REST API に送る JSON をそのまま反映します:

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

ホストされた サーバー は ツール を自動的に公開します。`mcp_servers` に追加する必要はありません。

### Streaming hosted MCP results

ホスト型 ツール は、関数ツール とまったく同じ方法で ストリーミング する 実行結果 に対応しています。`Runner.run_streamed` に `stream=True` を渡すと、モデルが処理を続けている間に増分的な MCP 出力を消費できます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

サーバー が機微な操作を実行できる場合、各ツール実行の前に人間またはプログラムによる承認を必須にできます。`tool_config` の `require_approval` を単一のポリシー（`"always"`、`"never"`）または ツール 名からポリシーへの dict で設定します。判断を Python 内で行うには、`on_approval_request` コールバックを指定します。

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

このコールバックは同期・非同期のどちらでもよく、モデルが継続実行に必要な承認データを求めるたびに呼び出されます。

### Connector-backed hosted servers

ホスト型 MCP は OpenAI コネクタにも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホストされた サーバー がコネクタの ツール を公開します。

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

ストリーミング、承認、コネクタを含む完全なホスト型 ツール のサンプルは、
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、レイテンシを低く保ちながら自分のインフラ内で サーバー を実行したい場合に最適です。

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

コンストラクタは次のオプションを受け付けます:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力よりも `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` により、一部の ツール のみを公開できます（[ツールのフィルタリング](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨としています。新規の連携では Streamable HTTP または stdio を優先し、SSE はレガシー サーバー のみで使用してください。

MCP サーバー が HTTP with SSE トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

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

ローカルのサブプロセスとして実行する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたままにし、コンテキストマネージャーの終了時に自動で閉じます。これは、短時間でのプロトタイピングや、サーバーがコマンドラインのエントリポイントのみを公開している場合に役立ちます。

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

## Tool filtering

各 MCP サーバーは ツール フィルターをサポートしており、エージェント に必要な関数だけを公開できます。フィルタリングは構築時にも、実行ごとに動的にも行えます。

### Static tool filtering

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、単純な許可/拒否リストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` が両方指定された場合、SDK はまず許可リストを適用し、その後、残りの集合からブロック対象の ツール を除外します。

### Dynamic tool filtering

より詳細なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能なオブジェクトを渡します。これは同期・非同期どちらでもよく、当該 ツール を公開すべき場合に `True` を返します。

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

フィルターコンテキストは、アクティブな `run_context`、ツールを要求している `agent`、および `server_name` を公開します。

## Prompts

MCP サーバーは、エージェントの instructions を動的に生成する Prompts も提供できます。Prompts に対応する サーバー は、次の 2 つのメソッドを公開します:

- `list_prompts()` は、利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じて パラメーター を指定して具体的なプロンプトを取得します。

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

## Caching

すべての エージェント 実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモート サーバー は顕著なレイテンシを生む可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。あとで新しい一覧を強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## Tracing

[トレーシング](./tracing.md) は MCP のアクティビティを自動的に捕捉します。含まれる内容:

1. ツール一覧のための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む、完全なホスト型 MCP のデモ。