---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが言語モデルにツールとコンテキストを公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は AI アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C が各種周辺機器やアクセサリーにデバイスを接続するための標準化された方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続するための標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、ファイルシステム、HTTP、またはコネクターをバックエンドとするツールをエージェントに公開するための独自サーバーを構築したりできます。

## MCP 統合の選択

MCP サーバーをエージェントに組み込む前に、ツール呼び出しをどこで実行するか、どのトランスポートに到達できるかを決めてください。以下のマトリクスは Python SDK がサポートする選択肢を要約しています。

| 必要なもの | 推奨オプション |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| モデルの代わりに OpenAI の Responses API から公開到達可能な MCP サーバーを呼び出す | [`HostedMCPTool`][agents.tool.HostedMCPTool] を介した **Hosted MCP server tools** |
| ローカルまたはリモートで実行する Streamable HTTP サーバーに接続する | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を介した **Streamable HTTP MCP servers** |
| Server-Sent Events を伴う HTTP を実装したサーバーと通信する | [`MCPServerSse`][agents.mcp.server.MCPServerSse] を介した **HTTP with SSE MCP servers** |
| ローカルプロセスを起動して stdin/stdout 経由で通信する | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を介した **stdio MCP servers** |

以下のセクションでは、各オプション、設定方法、どのトランスポートを優先すべきかを説明します。

## エージェントレベルの MCP 設定

トランスポートの選択に加えて、`Agent.mcp_config` を設定することで MCP ツールの準備方法を調整できます。

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    mcp_servers=[server],
    mcp_config={
        # Try to convert MCP tool schemas to strict JSON schema.
        "convert_schemas_to_strict": True,
        # If None, MCP tool failures are raised as exceptions instead of
        # returning model-visible error text.
        "failure_error_function": None,
    },
)
```

注意:

- `convert_schemas_to_strict` はベストエフォートです。スキーマを変換できない場合は元のスキーマが使用されます。
- `failure_error_function` は MCP ツール呼び出し失敗をモデルにどう提示するかを制御します。
- `failure_error_function` が未設定の場合、SDK はデフォルトのツールエラーフォーマッターを使用します。
- サーバーレベルの `failure_error_function` は、そのサーバーについて `Agent.mcp_config["failure_error_function"]` を上書きします。

## トランスポート間の共通パターン

トランスポートを選んだ後、ほとんどの統合で同じ追加判断が必要です。

- ツールの一部だけを公開する方法（[ツールフィルタリング](#tool-filtering)）。
- サーバーが再利用可能なプロンプトも提供するかどうか（[プロンプト](#prompts)）。
- `list_tools()` をキャッシュすべきかどうか（[キャッシュ](#caching)）。
- MCP のアクティビティがトレースにどう表示されるか（[トレーシング](#tracing)）。

ローカル MCP サーバー（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`）では、承認ポリシーと呼び出しごとの `_meta` ペイロードも共通概念です。Streamable HTTP セクションに最も完全なコード例があり、同じパターンが他のローカルトランスポートにも適用されます。

## 1. Hosted MCP server tools

Hosted ツールは、ツールの往復処理全体を OpenAI のインフラに移します。コード側でツールを一覧・呼び出しする代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] はサーバーラベル（および任意のコネクターメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを一覧し、Python プロセスへの追加コールバックなしで実行します。Hosted ツールは現在、Responses API の hosted MCP 統合をサポートする OpenAI モデルで動作します。

### 基本的な hosted MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して hosted ツールを作成します。`tool_config` の辞書は REST API に送る JSON を反映します。

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

Hosted サーバーは自動的にツールを公開するため、`mcp_servers` に追加する必要はありません。

### Hosted MCP 結果のストリーミング

Hosted ツールは、関数ツールとまったく同じ方法で結果のストリーミングをサポートします。`Runner.run_streamed` を使って、モデルがまだ処理中の間に増分 MCP 出力を消費します。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機密操作を実行できる場合、各ツール実行前に人間またはプログラムによる承認を必須にできます。`tool_config` の `require_approval` に、単一ポリシー（`"always"`、`"never"`）またはツール名からポリシーへの辞書を設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

このコールバックは同期または非同期にでき、モデルが実行継続のために承認データを必要とするたびに呼び出されます。

### コネクターをバックエンドとする hosted サーバー

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

ネットワーク接続を自身で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御したい場合や、低レイテンシを維持しつつ自社インフラ内でサーバーを実行したい場合に最適です。

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

コンストラクターは追加オプションを受け取ります。

- `client_session_timeout_seconds` は HTTP 読み取りタイムアウトを制御します。
- `use_structured_content` は `tool_result.structured_content` をテキスト出力より優先するかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は `list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` はツールの一部のみを公開できます（[ツールフィルタリング](#tool-filtering) を参照）。
- `require_approval` はローカル MCP ツールで human-in-the-loop 承認ポリシーを有効化します。
- `failure_error_function` はモデルに表示される MCP ツール失敗メッセージをカスタマイズします。代わりにエラーを送出するには `None` に設定します。
- `tool_meta_resolver` は `call_tool()` 前に呼び出しごとの MCP `_meta` ペイロードを注入します。

### ローカル MCP サーバー向け承認ポリシー

`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` はすべて `require_approval` を受け付けます。

サポートされる形式:

- すべてのツールに対する `"always"` または `"never"`。
- `True` / `False`（always/never と同等）。
- ツールごとのマップ（例: `{"delete_file": "always", "read_file": "never"}`）。
- グループ化オブジェクト:
  `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

完全な一時停止/再開フローは [Human-in-the-loop](human_in_the_loop.md) と `examples/mcp/get_all_mcp_tools_example/main.py` を参照してください。

### `tool_meta_resolver` を使った呼び出しごとのメタデータ

MCP サーバーが `_meta` にリクエストメタデータ（例: テナント ID やトレースコンテキスト）を期待する場合は `tool_meta_resolver` を使います。以下の例は、`Runner.run(...)` に `context` として `dict` を渡す前提です。

```python
from agents.mcp import MCPServerStreamableHttp, MCPToolMetaContext


def resolve_meta(context: MCPToolMetaContext) -> dict[str, str] | None:
    run_context_data = context.run_context.context or {}
    tenant_id = run_context_data.get("tenant_id")
    if tenant_id is None:
        return None
    return {"tenant_id": str(tenant_id), "source": "agents-sdk"}


server = MCPServerStreamableHttp(
    name="Metadata-aware MCP",
    params={"url": "http://localhost:8000/mcp"},
    tool_meta_resolver=resolve_meta,
)
```

実行コンテキストが Pydantic モデル、dataclass、またはカスタムクラスの場合は、代わりに属性アクセスでテナント ID を読み取ってください。

### MCP ツール出力: テキストと画像

MCP ツールが画像コンテンツを返す場合、SDK はそれを画像ツール出力エントリーに自動的にマッピングします。テキスト/画像の混在レスポンスは出力アイテムのリストとして転送されるため、エージェントは通常の関数ツールの画像出力と同じ方法で MCP の画像結果を扱えます。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨にしています。新規統合では Streamable HTTP または stdio を優先し、SSE はレガシーサーバーでのみ維持してください。

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポートを除き、API は Streamable HTTP サーバーと同一です。

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

ローカルサブプロセスとして動作する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキストマネージャー終了時に自動で閉じます。このオプションは素早い概念実証や、サーバーがコマンドラインエントリーポイントのみを公開している場合に有用です。

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

複数の MCP サーバーがある場合、`MCPServerManager` を使って事前に接続し、接続済みのサブセットをエージェントに公開します。コンストラクターオプションと再接続動作は [MCPServerManager API リファレンス](ref/mcp/manager.md) を参照してください。

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

主要な動作:

- `drop_failed_servers=True`（デフォルト）の場合、`active_servers` には接続成功したサーバーのみが含まれます。
- 失敗は `failed_servers` と `errors` で追跡されます。
- 最初の接続失敗で例外を送出するには `strict=True` を設定します。
- 失敗サーバーを再試行するには `reconnect(failed_only=True)`、全サーバーを再起動するには `reconnect(failed_only=False)` を呼び出します。
- ライフサイクル動作の調整には `connect_timeout_seconds`、`cleanup_timeout_seconds`、`connect_in_parallel` を使用します。

## 一般的なサーバー機能

以下のセクションは MCP サーバートランスポート全体に適用されます（正確な API サーフェスはサーバークラスに依存します）。

## ツールフィルタリング

各 MCP サーバーはツールフィルターをサポートしており、エージェントに必要な関数のみを公開できます。フィルタリングは構築時、または実行ごとに動的に行えます。

### 静的ツールフィルタリング

シンプルな許可/ブロックリストを設定するには [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後残りの集合からブロック対象ツールを削除します。

### 動的ツールフィルタリング

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る callable を渡します。callable は同期/非同期のどちらでもよく、ツールを公開すべき場合に `True` を返します。

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

MCP サーバーは、エージェント指示を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは 2 つのメソッドを公開します。

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じてパラメーターを使って具体的なプロンプトを取得します。

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

各エージェント実行は各 MCP サーバーで `list_tools()` を呼び出します。リモートサーバーでは目立つレイテンシが発生する可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合のみ `True` に設定してください。後で強制的に最新一覧を取得するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[トレーシング](./tracing.md) は以下を含む MCP アクティビティを自動で記録します。

1. ツール一覧取得のための MCP サーバー呼び出し。
2. ツール呼び出し時の MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 追加資料

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクターを含む完全な hosted MCP デモ。