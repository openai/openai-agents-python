---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが言語モデルに対してツールやコンテキストを公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンなプロトコルです。MCP は、AI アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリに接続するための標準化された方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続するための標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、独自のサーバーを構築して、ファイルシステム、HTTP、またはコネクターにより支えられたツールを エージェント に公開したりできます。

## MCP 統合の選択

MCP サーバーを エージェント に組み込む前に、ツール呼び出しをどこで実行するか、そして到達可能なトランスポートがどれかを決めてください。以下のマトリクスは、Python SDK がサポートする選択肢を要約したものです。

| 必要なもの                                                                        | 推奨オプション                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API に、モデルの代わりに公開到達可能な MCP サーバーを呼び出させる| [`HostedMCPTool`][agents.tool.HostedMCPTool] 経由の **Hosted MCP server tools** |
| ローカルまたはリモートで実行する Streamable HTTP サーバーに接続する                  | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 経由の **Streamable HTTP MCP servers** |
| Server-Sent Events を用いた HTTP を実装するサーバーと通信する                          | [`MCPServerSse`][agents.mcp.server.MCPServerSse] 経由の **HTTP with SSE MCP servers** |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                             | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 経由の **stdio MCP servers** |

以下のセクションでは、それぞれの選択肢、設定方法、そしてどのトランスポートを優先すべきかを説明します。

## 1. Hosted MCP server tools

Hosted ツールは、ツールの往復処理全体を OpenAI のインフラストラクチャに移します。コード側でツールを列挙して呼び出すのではなく、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーラベル（および任意のコネクターメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを列挙して呼び出し、Python プロセスへの追加コールバックなしに実行します。Hosted ツールは現在、Responses API の hosted MCP 統合をサポートする OpenAI モデルで動作します。

### 基本の hosted MCP ツール

エージェント の `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して hosted ツールを作成します。`tool_config` の dict は、REST API に送る JSON を反映します。

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

hosted サーバーはツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

### hosted MCP 結果のストリーミング

Hosted ツールは、関数ツールとまったく同じ方法で結果のストリーミングをサポートします。`Runner.run_streamed` に `stream=True` を渡して、モデルがまだ作業中の間に増分の MCP 出力を消費します。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機微な操作を実行できる場合、各ツール実行の前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を、単一のポリシー（`"always"`, `"never"`）またはツール名からポリシーへの dict のいずれかで設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

このコールバックは同期または非同期にでき、モデルが実行を継続するために承認データを必要とするたびに呼び出されます。

### コネクターにより支えられた hosted サーバー

Hosted MCP は OpenAI コネクターもサポートします。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、hosted サーバーがコネクターのツールを公開します。

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

ストリーミング、承認、コネクターを含む完全に動作する hosted ツールのサンプルは、[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御している場合や、レイテンシを低く保ちながら自社インフラ内でサーバーを実行したい場合に最適です。

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

コンストラクタは追加のオプションを受け取ります:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力より `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` の自動リトライを追加します。
- `tool_filter` は、一部のツールのみを公開できます（[ツールのフィルタリング](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨にしました。新規統合では Streamable HTTP または stdio を優先し、SSE はレガシーサーバー向けのみにしてください。

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

ローカルのサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを生成し、パイプを開いたままにし、コンテキストマネージャーの終了時に自動で閉じます。この選択肢は、迅速な概念実証や、サーバーがコマンドラインのエントリポイントしか公開しない場合に役立ちます。

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

## 5. MCP サーバーマネージャー

複数の MCP サーバーがある場合は、`MCPServerManager` を使って事前に接続し、接続済みのサブセットを エージェント に公開します。

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

主な挙動:

- `drop_failed_servers=True`（デフォルト）の場合、`active_servers` には接続に成功したサーバーのみが含まれます。
- 失敗は `failed_servers` と `errors` に追跡されます。
- `strict=True` を設定すると、最初の接続失敗で例外を送出します。
- 失敗したサーバーを再試行するには `reconnect(failed_only=True)` を呼び出し、すべてのサーバーを再起動するには `reconnect(failed_only=False)` を呼び出します。
- `connect_timeout_seconds`、`cleanup_timeout_seconds`、`connect_in_parallel` を使ってライフサイクルの挙動を調整します。

## ツールのフィルタリング

各 MCP サーバーはツールフィルターをサポートしており、エージェント が必要とする関数だけを公開できます。フィルタリングは構築時、または実行ごとに動的に行えます。

### 静的ツールフィルタリング

簡単な許可/ブロックリストを設定するには、[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定されている場合、SDK は最初に許可リストを適用し、その後、残った集合からブロックされたツールを削除します。

### 動的ツールフィルタリング

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る callable を渡します。callable は同期または非同期にでき、ツールを公開すべき場合に `True` を返します。

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

## プロンプト

MCP サーバーは、エージェント の指示を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは、次の 2 つのメソッドを公開します:

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は具体的なプロンプトを取得します（任意でパラメーター付き）。

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

エージェント の各実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモートサーバーは目に見えるレイテンシをもたらす可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁には変わらないと確信できる場合にのみ、これを `True` に設定してください。後で新しいリストを強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[Tracing](./tracing.md) は、以下を含む MCP アクティビティを自動的にキャプチャします:

1. ツールを列挙するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## さらに読む

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクターを含む、Hosted MCP の完全なデモ。