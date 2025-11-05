---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールとコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントからの抜粋です。

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK は複数の MCP トランスポートに対応しています。これにより既存の MCP サーバーを再利用したり、独自に構築して、ファイルシステム、HTTP、またはコネクターをバックエンドに持つツールを エージェント に公開できます。

## Choosing an MCP integration

MCP サーバーを エージェント に組み込む前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めます。以下のマトリクスは Python SDK がサポートするオプションの概要です。

| 必要なこと                                                                             | 推奨オプション                                              |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| OpenAI の Responses API がモデルに代わって外部到達可能な MCP サーバーを呼び出す                |  **ホスト型 MCP サーバー ツール**  via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| ローカルまたはリモートで稼働中の Streamable HTTP サーバーに接続する                           |  **Streamable HTTP MCP サーバー**  via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Server-Sent Events を用いた HTTP を実装するサーバーと通信する                                   |  **HTTP with SSE MCP サーバー**  via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                                          |  **stdio MCP サーバー**  via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下のセクションでは、それぞれのオプションについて、設定方法と使い分けの目安を説明します。

## 1. Hosted MCP server tools

ホスト型ツールはツールの往復処理全体を OpenAI のインフラに委ねます。あなたのコードでツールの列挙や呼び出しを行う代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] が サーバー ラベル（および任意のコネクター メタデータ）を Responses API に転送します。モデルはリモート サーバーのツールを列挙し、あなたの Python プロセスへの追加のコールバックなしにそれらを呼び出します。ホスト型ツールは現在、Responses API のホスト型 MCP 連携をサポートする OpenAI モデルで動作します。

### Basic hosted MCP tool

エージェント の `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加してホスト型ツールを作成します。`tool_config` の dict は REST API に送る JSON と同じ構造です。

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

ホスト型サーバーはツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

### Streaming hosted MCP results

ホスト型ツールは 関数ツール とまったく同じ方法で ストリーミング に対応しています。`Runner.run_streamed` に `stream=True` を渡すと、モデルが処理中でも段階的な MCP 出力を消費できます。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

サーバーが機微な操作を行う場合、各ツール実行の前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` に単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict を設定します。判断を Python 内で行うには、`on_approval_request` コールバックを指定します。

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

コールバックは同期または非同期のいずれでもよく、モデルが継続実行のために承認データを必要とするたびに呼び出されます。

### Connector-backed hosted servers

ホスト型 MCP は OpenAI connectors にも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト型サーバーがコネクターのツールを公開します。

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

ストリーミング、承認、コネクターを含む完全なホスト型ツールのサンプルは
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、自社インフラ内でサーバーを稼働させつつ低レイテンシを維持したい場合に最適です。

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

コンストラクターは追加オプションを受け付けます。

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は `tool_result.structured_content` をテキスト出力より優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は `list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` は公開するツールをサブセットに限定できます（[ツール フィルタリング](#tool-filtering) を参照）。

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

ローカルのサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK がプロセスを起動してパイプを開いたまま維持し、コンテキストマネージャーが終了すると自動的にクローズします。コマンドライン エントリポイントのみを公開するサーバーや、迅速なプロトタイプに便利です。

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

各 MCP サーバーはツール フィルターをサポートし、エージェント に必要な関数だけを公開できます。フィルタリングは構築時にも実行ごとにも行えます。

### Static tool filtering

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、シンプルな許可/ブロック リストを設定します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK は先に許可リストを適用し、その後に残りの集合からブロック対象を取り除きます。

### Dynamic tool filtering

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出し可能オブジェクトは同期または非同期のいずれでもよく、ツールを公開すべき場合に `True` を返します。

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

フィルター コンテキストは、アクティブな `run_context`、ツールを要求している `agent`、および `server_name` を公開します。

## Prompts

MCP サーバーは エージェント の instructions を動的に生成する プロンプト も提供できます。プロンプトに対応するサーバーは次の 2 つのメソッドを公開します。

- `list_prompts()` は利用可能なプロンプト テンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じてパラメーター付きで具体的なプロンプトを取得します。

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

各 エージェント 実行は、それぞれの MCP サーバーに対して `list_tools()` を呼び出します。リモート サーバーは目立つレイテンシを生む可能性があるため、すべての MCP サーバー クラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。後で最新のリストを強制するには、サーバー インスタンスで `invalidate_tools_cache()` を呼び出します。

## Tracing

[トレーシング](./tracing.md) は MCP のアクティビティを自動的に捕捉し、次を含みます。

1. ツールを列挙するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクターを含む完全なホスト型 MCP デモ。