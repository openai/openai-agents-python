---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールやコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、独自に構築してファイルシステム、HTTP、あるいはコネクタ バックエンドのツールをエージェントに公開できます。

## MCP 統合の選択

MCP サーバーをエージェントに接続する前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めます。以下の表は Python SDK がサポートするオプションの概要です。

| 要件                                                                                  | 推奨オプション                                           |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| OpenAI の Responses API がモデルの代理でインターネット経由で到達可能な MCP サーバーを呼び出す | **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| ローカルまたはリモートで実行する Streamable HTTP サーバーに接続する                     | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server-Sent Events を備えた HTTP を実装するサーバーと通信する                          | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| ローカル プロセスを起動し、stdin/stdout 経由で通信する                                 | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下のセクションではそれぞれのオプションについて、設定方法やどのトランスポートを選ぶべきかを解説します。

## 1. Hosted MCP サーバーのツール

ホスト型ツールは、ツールの往復処理全体を OpenAI のインフラに委ねます。あなたのコードがツールを列挙・呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーのラベル（および任意のコネクタ メタデータ）を Responses API に転送します。モデルはリモート サーバーのツールを列挙し、あなたの Python プロセスへの追加のコールバックなしにそれらを呼び出します。ホスト型ツールは現在、Responses API の ホスト型 MCP 統合をサポートする OpenAI のモデルで動作します。

### 基本的なホスト型 MCP ツール

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

ホスト型サーバーは自動的にツールを公開します。`mcp_servers` に追加する必要はありません。

### ホスト型 MCP 結果のストリーミング

ホスト型ツールは、関数ツールとまったく同じ方法で結果のストリーミングをサポートします。`Runner.run_streamed` に `stream=True` を渡すと、モデルが処理中でも増分の MCP 出力を消費できます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### オプションの承認フロー

サーバーが機微な操作を実行できる場合、各ツール実行の前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を単一ポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict で設定してください。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

コールバックは同期または非同期のいずれでもよく、モデルが継続実行に必要な承認データを求めるたびに呼び出されます。

### コネクタ バックエンドのホスト型サーバー

ホスト型 MCP は OpenAI コネクタもサポートします。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト型サーバーはコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む完全なホスト型ツールのサンプルは
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP サーバー

ネットワーク接続を自分で管理したい場合は
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御したい場合や、レイテンシを抑えつつ自社インフラ内でサーバーを稼働させたい場合に最適です。

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

コンストラクタは次の追加オプションを受け付けます:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力より `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` により、公開するツールをサブセットに限定できます（[Tool filtering](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP サーバー

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、
[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

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

ローカル サブプロセスとして実行する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキスト マネージャの終了時に自動的にクローズします。これは、迅速なプロトタイプや、サーバーがコマンドライン エントリ ポイントのみを公開する場合に有用です。

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

各 MCP サーバーはツール フィルタをサポートしており、エージェントに必要な機能だけを公開できます。フィルタリングは構築時にも、実行ごとに動的にも行えます。

### 静的なツール フィルタリング

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後に残りの集合から拒否ツールを取り除きます。

### 動的なツール フィルタリング

より精緻なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出し可能オブジェクトは同期または非同期のいずれでもよく、ツールを公開すべき場合に `True` を返します。

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

フィルタ コンテキストは、アクティブな `run_context`、ツールを要求している `agent`、および `server_name` を公開します。

## プロンプト

MCP サーバーは、エージェントの指示を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは、次の 2 つのメソッドを公開します:

- `list_prompts()` は利用可能なプロンプト テンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じてパラメーター付きで具象プロンプトを取得します。

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

すべてのエージェント実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモート サーバーは顕著なレイテンシを導入する可能性があるため、すべての MCP サーバー クラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変更されないと確信できる場合にのみ `True` に設定してください。後で新しいリストを強制したい場合は、サーバー インスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[トレーシング](./tracing.md) は MCP のアクティビティを自動的に記録します。含まれる内容:

1. MCP サーバーへのツール一覧取得呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## 参考情報

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、および Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む完全なホスト型 MCP デモ。