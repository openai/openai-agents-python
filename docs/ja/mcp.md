---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールやコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンなプロトコルです。MCP は AI アプリケーションのための USB‑C ポートのようなものだと考えてください。USB‑C がデバイスをさまざまな周辺機器やアクセサリに接続する標準化された方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続する標準化された方法を提供します。

Agents の Python SDK は複数の MCP トランスポートに対応しています。これにより、既存の MCP サーバーを再利用したり、独自に構築して、ファイルシステム、HTTP、またはコネクタをバックエンドに持つツールを エージェント に公開できます。

## MCP 統合の選択

MCP サーバーを エージェント に接続する前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めてください。以下のマトリクスは、Python SDK がサポートするオプションの概要です。

| 必要なこと                                                                            | 推奨オプション                                           |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| OpenAI の Responses API に、モデルの代わりにパブリック到達可能な MCP サーバーを呼び出させる | **Hosted MCP server tools** 経由の [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| ローカルまたはリモートで実行する Streamable な HTTP サーバーに接続する                  | **Streamable HTTP MCP servers** 経由の [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server‑Sent Events を実装した HTTP サーバーと通信する                                 | **HTTP with SSE MCP servers** 経由の [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                                 | **stdio MCP servers** 経由の [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下のセクションでは、それぞれのオプションについて、設定方法と、どのトランスポートを選ぶべきかを説明します。

## 1. Hosted MCP server tools

Hosted ツールは、ツールの往復をすべて OpenAI のインフラストラクチャ内で完結させます。あなたのコードがツールの列挙と呼び出しを行う代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] はサーバーラベル（および任意のコネクタメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを一覧し、あなたの Python プロセスへの余分なコールバックなしにそれらを呼び出します。Hosted ツールは現在、Responses API の hosted MCP 統合をサポートする OpenAI モデルで動作します。

### 基本の hosted MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して hosted ツールを作成します。`tool_config` の dict は、REST API に送信する JSON と同じ構造です:

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

### ストリーミングされる hosted MCP の結果

Hosted ツールは、関数ツールとまったく同じ方法で結果の ストリーミング をサポートします。`Runner.run_streamed` に `stream=True` を渡して、モデルが処理中の増分 MCP 出力を消費します:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機微な操作を実行できる場合、各ツール実行の前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を、単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict で設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

コールバックは同期・非同期のどちらでもよく、モデルが実行を継続するために承認データを必要とするたびに呼び出されます。

### コネクタをバックエンドに持つ hosted サーバー

Hosted MCP は OpenAI コネクタにも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホストされたサーバーがコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む完全な Hosted ツールのサンプルは、
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP サーバー

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable な HTTP サーバーは、トランスポートを自分で制御したい場合や、低レイテンシを維持しながら自分のインフラ内でサーバーを実行したい場合に最適です。

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
- `tool_filter` により、ツールの一部だけを公開できます（[ツールのフィルタリング](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP サーバー

MCP サーバーが HTTP with SSE のトランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

```python

from agents import Agent, Runner
from agents.model_settings import ModelSettings
from mcp import MCPServerSse

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

ローカルのサブプロセスとして実行する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたままにし、コンテキストマネージャーの終了時に自動的に閉じます。これは、プロトタイピングや、サーバーがコマンドラインのエントリポイントのみを公開する場合に役立ちます。

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

各 MCP サーバーはツールフィルターをサポートしており、エージェント が必要とする関数だけを公開できます。フィルタリングは構築時にも、実行ごとに動的にも行えます。

### 静的なツールフィルタリング

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、簡単な許可/ブロックリストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定されている場合、SDK はまず許可リストを適用し、その後、残りの集合からブロックされたツールを削除します。

### 動的なツールフィルタリング

より高度なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出し可能オブジェクトは同期・非同期のどちらでもよく、ツールを公開すべき場合に `True` を返します。

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

フィルターコンテキストは、アクティブな `run_context`、ツールを要求している `agent`、`server_name` を公開します。

## プロンプト

MCP サーバーは、エージェントの instructions を動的に生成するプロンプトも提供できます。プロンプトに対応するサーバーは 2 つのメソッドを公開します:

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

すべてのエージェント実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモートサーバーは目立つレイテンシをもたらす可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。後から新しい一覧を強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[Tracing](./tracing.md) は MCP のアクティビティを自動的に捕捉します。以下を含みます:

1. ツール一覧のための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 参考資料

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む完全な hosted MCP のデモ。