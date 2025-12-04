---
search:
  exclude: true
---
# Model context protocol (MCP)

The [Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが ツール とコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLMs にコンテキストを提供する方法を標準化するオープンなプロトコルです。MCP は AI アプリケーションにおける USB‑C ポートのようなものだと考えてください。USB‑C がさまざまな周辺機器やアクセサリにデバイスを接続する標準化された方法を提供するように、MCP は AI モデルを異なるデータソースやツールに接続する標準化された方法を提供します。

The Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、独自に構築して、ファイルシステム、HTTP、またはコネクタを基盤とするツールを エージェント に公開できます。

## Choosing an MCP integration

エージェントに MCP サーバーを接続する前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めます。以下のマトリクスは、Python SDK がサポートするオプションの概要です。

| What you need                                                                        | Recommended option                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Let OpenAI's Responses API call a publicly reachable MCP server on the model's behalf| **Hosted MCP server tools** via [`HostedMCPTool`][agents.tool.HostedMCPTool] |
| Connect to Streamable HTTP servers that you run locally or remotely                  | **Streamable HTTP MCP servers** via [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] |
| Talk to servers that implement HTTP with Server-Sent Events                          | **HTTP with SSE MCP servers** via [`MCPServerSse`][agents.mcp.server.MCPServerSse] |
| Launch a local process and communicate over stdin/stdout                             | **stdio MCP servers** via [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] |

以下のセクションでは、各オプションの設定方法と、どのような場面でどのトランスポートを優先すべきかを説明します。

## 1. Hosted MCP server tools

ホスト型ツールでは、ツールの往復処理全体を OpenAI のインフラに任せます。あなたのコードがツールの列挙と呼び出しを行う代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] が サーバー ラベル（および任意のコネクタ メタデータ）を Responses API に転送します。モデルはリモート サーバーのツールを列挙し、あなたの Python プロセスへの追加のコールバックなしでそれらを呼び出します。ホスト型ツールは現在、Responses API のホスト型 MCP 連携をサポートする OpenAI モデルで動作します。

### Basic hosted MCP tool

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型ツールを作成します。`tool_config` の dict は、REST API に送信する JSON を反映します:

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

ホスト サーバーはそのツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

### Streaming hosted MCP results

ホスト型ツールは、関数ツールとまったく同じ方法で ストリーミング 結果をサポートします。`Runner.run_streamed` に `stream=True` を渡して、モデルがまだ実行中の間に増分的な MCP 出力を消費します:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### Optional approval flows

サーバーが機微な操作を実行できる場合、各ツール実行の前に人手またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を、単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict で設定します。Python 内で意思決定するには、`on_approval_request` コールバックを指定します。

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

コールバックは同期または非同期のいずれでもよく、モデルが実行を続けるために承認データを必要とするたびに呼び出されます。

### Connector-backed hosted servers

ホスト型 MCP は OpenAI コネクタにも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト サーバーがコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む完全なホスト型ツールのサンプルは、
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、
[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御したい場合や、レイテンシを低く保ちながら自分のインフラ内でサーバーを実行したい場合に最適です。

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
- `tool_filter` により、公開するツールのサブセットのみを露出できます（[ツール フィルタリング](#tool-filtering) を参照）。

## 3. HTTP with SSE MCP servers

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、
[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

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

ローカルのサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキスト マネージャの終了時に自動的に閉じます。このオプションは、迅速なプロトタイプ作成や、サーバーがコマンドライン エントリポイントのみを公開している場合に便利です。

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

各 MCP サーバーはツール フィルタをサポートしており、エージェントに必要な関数のみを公開できます。フィルタリングは、構築時にも、実行ごとに動的にも行えます。

### Static tool filtering

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、シンプルな許可/ブロック リストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後、残りの集合からブロック対象のツールを削除します。

### Dynamic tool filtering

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る呼び出し可能オブジェクトを渡します。呼び出し可能オブジェクトは同期または非同期でよく、ツールを公開すべき場合に `True` を返します。

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

フィルタ コンテキストは、アクティブな `run_context`、ツールを要求する `agent`、および `server_name` を提供します。

## Prompts

MCP サーバーは、エージェントの instructions を動的に生成する プロンプト も提供できます。プロンプトをサポートするサーバーは、次の 2 つのメソッドを公開します:

- `list_prompts()` は、利用可能なプロンプト テンプレートを列挙します。
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

すべてのエージェント実行は、各 MCP サーバーに対して `list_tools()` を呼び出します。リモート サーバーは顕著なレイテンシをもたらす可能性があるため、すべての MCP サーバー クラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変更されないと確信できる場合にのみ、これを `True` に設定してください。後で最新のリストを強制するには、サーバー インスタンスで `invalidate_tools_cache()` を呼び出します。

## Tracing

[トレーシング](./tracing.md) は、以下を含む MCP のアクティビティを自動的に捕捉します:

1. ツールを列挙するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクタを含む、完全なホスト型 MCP デモ。