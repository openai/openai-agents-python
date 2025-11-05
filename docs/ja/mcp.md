---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールとコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントからの引用です。

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は、AI アプリケーションにおける USB‑C ポートのようなものだと考えてください。USB‑C が、デバイスをさまざまな周辺機器やアクセサリーに接続するための標準化された方法を提供するのと同様に、MCP は、AI モデルを異なるデータソースやツールに接続するための標準化された方法を提供します。

Agents の Python SDK は、複数の MCP トランスポートに対応しています。これにより、既存の MCP サーバーを再利用したり、独自のサーバーを構築して、ファイルシステム、HTTP、またはコネクタをバックエンドに持つツールを エージェント に公開できます。

## Choosing an MCP integration

MCP サーバーを エージェント に接続する前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めます。以下のマトリクスは、Python SDK がサポートするオプションの概要です。

| 必要なこと                                                                             | 推奨オプション                                             |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| OpenAI の Responses API が、モデルの代わりに公開到達可能な MCP サーバーを呼び出す     | **Hosted MCP server tools**（ホスト型 MCP ツール）経由の [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| ローカルまたはリモートで運用する Streamable HTTP サーバーに接続する                  | **Streamable HTTP MCP servers** 経由の [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server‑Sent Events を備えた HTTP を実装するサーバーと通信する                         | **HTTP with SSE MCP servers** 経由の [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                                 | **stdio MCP servers** 経由の [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下のセクションでは、それぞれの選択肢、設定方法、およびどのトランスポートを選ぶべきかについて説明します。

## 1. Hosted MCP server tools

ホスト型ツールは、ツールの往復処理全体を OpenAI のインフラに委ねます。あなたのコードがツールを列挙・呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] が サーバーラベル（および任意のコネクタ メタデータ）を Responses API に転送します。モデルは、リモートサーバーのツールを列挙し、あなたの Python プロセスへの追加のコールバックなしにそれらを実行します。ホスト型ツールは現在、Responses API の hosted MCP 連携をサポートする OpenAI モデルで動作します。

### Basic hosted MCP tool

エージェント の `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型ツールを作成します。`tool_config` 辞書は、REST API に送信する JSON と同じ構造です。

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

ホストされたサーバーは、自動的に自身のツールを公開します。`mcp_servers` に追加する必要はありません。

### Streaming hosted MCP results

ホスト型ツールは、関数ツールとまったく同じ方法で ストリーミング に対応します。`Runner.run_streamed` に `stream=True` を渡して、モデルが処理を続けている間にインクリメンタルな MCP 出力を取り込みます。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

サーバーが機密操作を実行できる場合、各ツール実行の前に、人間またはプログラムによる承認を必須にできます。`tool_config` の `require_approval` を単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの辞書で設定します。判断を Python 内で行うには、`on_approval_request` コールバックを指定します。

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

コールバックは同期・非同期いずれでもかまいません。モデルが実行継続のために承認データを必要とするたびに呼び出されます。

### Connector-backed hosted servers

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

ストリーミング、承認、コネクタを含む完全なホスト型ツールのサンプルは、[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、低レイテンシを維持しつつ自社インフラ内でサーバーを運用したい場合に最適です。

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

コンストラクターは、追加のオプションを受け付けます。

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力よりも `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` により、公開するツールを一部に限定できます（[Tool filtering](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP servers

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポートを除けば、API は Streamable HTTP サーバーと同一です。

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

ローカルのサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキストマネージャーの終了時に自動でクローズします。これは、迅速なプロトタイプ作成や、サーバーがコマンドラインのエントリポイントのみを公開する場合に有用です。

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

各 MCP サーバーはツールのフィルタリングに対応しており、エージェント が必要とする機能だけを公開できます。フィルタリングは、構築時にも実行単位でも行えます。

### Static tool filtering

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、簡単な許可／ブロックリストを設定します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後に残りの集合からブロック対象のツールを除外します。

### Dynamic tool filtering

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出しは同期・非同期のどちらでもよく、ツールを公開すべきときに `True` を返します。

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

MCP サーバーは、エージェント の instructions を動的に生成する Prompts も提供できます。Prompts をサポートするサーバーは、次の 2 つのメソッドを公開します。

- `list_prompts()` は、利用可能なプロンプトテンプレートを列挙します。
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

## Caching

すべての エージェント 実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモートサーバーは顕著なレイテンシをもたらす可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ、これを `True` に設定してください。後で新しいリストを強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## Tracing

[Tracing](./tracing.md) は、以下を含む MCP のアクティビティを自動的に取得します。

1. ツールを列挙するための MCP サーバーへの呼び出し
2. ツール呼び出しに関する MCP 関連情報

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む、完全な hosted MCP のデモ