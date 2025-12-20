---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールやコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLMs にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は AI アプリケーションにおける USB-C ポートのようなものだと考えてください。USB-C がさまざまな周辺機器やアクセサリーにデバイスを接続するための標準化された方法を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続するための標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートに対応しています。これにより、既存の MCP サーバーを再利用したり、独自に構築して、ファイルシステム、HTTP、またはコネクタに裏付けられたツールを エージェント に公開できます。

## MCP 統合の選択

MCP サーバーを エージェント に接続する前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めます。以下のマトリクスは、Python SDK がサポートするオプションの概要です。

| 必要なこと                                                                            | 推奨オプション                                         |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API がモデルの代わりに公開到達可能な MCP サーバーを呼び出す      | **ホスト型 MCP サーバーツール**（[`HostedMCPTool`][agents.tool.HostedMCPTool] 経由） |
| ローカルまたはリモートで稼働する Streamable HTTP サーバーに接続する                 | **Streamable HTTP MCP サーバー**（[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 経由） |
| Server-Sent Events を伴う HTTP を実装するサーバーと通信する                           | **HTTP with SSE MCP サーバー**（[`MCPServerSse`][agents.mcp.server.MCPServerSse] 経由） |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                                | **stdio MCP サーバー**（[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 経由） |

以下のセクションでは、それぞれのオプションの使い方、設定方法、およびどのトランスポートを選ぶべきかの判断基準を説明します。

## 1. Hosted MCP サーバーツール

ホスト型ツールは、ツールの往復処理全体を OpenAI のインフラストラクチャにオフロードします。あなたのコードがツールを列挙・呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] が サーバーラベル（および任意のコネクタメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを列挙し、あなたの Python プロセスへの追加のコールバックなしにそれらを呼び出します。ホスト型ツールは現在、Responses API の hosted MCP 統合をサポートする OpenAI モデルで動作します。

### 基本的な hosted MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加してホスト型ツールを作成します。`tool_config` の dict は、REST API に送信する JSON を反映します:

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

ホストされたサーバーはツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

### ストリーミング hosted MCP の実行結果

ホスト型ツールは 関数ツール とまったく同じ方法で ストリーミング の実行結果をサポートします。`Runner.run_streamed` に `stream=True` を渡すと、モデルがまだ実行中でも MCP の増分出力を消費できます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機微な操作を実行できる場合、各ツール実行の前に、人間またはプログラムによる承認を必須にできます。`tool_config` の `require_approval` を、単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict マップで設定します。判断を Python 内で行うには、`on_approval_request` コールバックを指定してください。

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

このコールバックは同期・非同期いずれでもよく、モデルが継続実行のために承認データを必要とするたびに呼び出されます。

### コネクタに裏付けられたホスト型サーバー

Hosted MCP は OpenAI コネクタにも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト型サーバーがコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む完全に動作するホスト型ツールのサンプルは、
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP サーバー

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、レイテンシを低く保ちながら自社インフラ内でサーバーを稼働させたい場合に最適です。

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

コンストラクターは以下の追加オプションを受け取ります:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力よりも `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に対する自動リトライを追加します。
- `tool_filter` により、公開するツールを一部のみに制限できます（[Tool filtering](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP サーバー

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

## 4. stdio MCP サーバー

ローカルのサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキストマネージャーの終了時に自動で閉じます。このオプションは、迅速なプロトタイプ作成や、サーバーがコマンドラインのエントリポイントのみを公開する場合に便利です。

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

各 MCP サーバーはツールフィルターをサポートしており、エージェント が必要とする関数のみを公開できます。フィルタリングは、構築時または実行ごとに動的に行えます。

### 静的なツールフィルタリング

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、シンプルな許可／ブロックリストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後で残りの集合からブロック対象のツールを削除します。

### 動的なツールフィルタリング

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。これは同期・非同期いずれでもよく、ツールを公開すべき場合に `True` を返します。

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

MCP サーバーは、エージェントの instructions を動的に生成する プロンプト も提供できます。プロンプトをサポートするサーバーは次の 2 つのメソッドを公開します:

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じて パラメーター を伴う具体的なプロンプトを取得します。

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

すべてのエージェント実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモートサーバーは顕著なレイテンシをもたらす可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ、これを `True` に設定してください。後で新しいリストを強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[トレーシング](./tracing.md) は MCP のアクティビティを自動的に捕捉し、以下を含みます:

1. MCP サーバーへのツール一覧取得の呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 参考資料

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む完全な hosted MCP のデモ。