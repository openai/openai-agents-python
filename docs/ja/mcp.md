---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが言語モデルにツールやコンテキストを公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は AI アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続するための標準化された方法を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続するための標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、独自に構築してファイルシステム、 HTTP 、またはコネクタをバックエンドとするツールをエージェントに公開したりできます。

## MCP 統合の選択

MCP サーバーをエージェントに接続する前に、ツール呼び出しをどこで実行するか、到達可能なトランスポートはどれかを決めてください。以下のマトリクスは、 Python SDK がサポートする選択肢を要約したものです。

| 必要なもの | 推奨オプション |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| モデルの代わりに OpenAI の Responses API から公開到達可能な MCP サーバーを呼び出す | [`HostedMCPTool`][agents.tool.HostedMCPTool] による **Hosted MCP server tools** |
| ローカルまたはリモートで実行している Streamable HTTP サーバーに接続する | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] による **Streamable HTTP MCP servers** |
| Server-Sent Events を使う HTTP を実装したサーバーと通信する | [`MCPServerSse`][agents.mcp.server.MCPServerSse] による **HTTP with SSE MCP servers** |
| ローカルプロセスを起動し stdin/stdout 経由で通信する | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] による **stdio MCP servers** |

以下のセクションでは、各オプション、設定方法、どのトランスポートを優先すべきかを説明します。

## エージェントレベルの MCP 設定

トランスポートの選択に加えて、 `Agent.mcp_config` を設定して MCP ツールの準備方法を調整できます。

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

注記:

- `convert_schemas_to_strict` はベストエフォートです。スキーマを変換できない場合は元のスキーマが使われます。
- `failure_error_function` は MCP ツール呼び出し失敗をモデルへどのように提示するかを制御します。
- `failure_error_function` が未設定の場合、 SDK はデフォルトのツールエラーフォーマッターを使います。
- サーバーレベルの `failure_error_function` は、そのサーバーに対して `Agent.mcp_config["failure_error_function"]` を上書きします。

## トランスポート間の共通パターン

トランスポートを選んだ後、ほとんどの統合で同じ追加判断が必要です:

- ツールの一部だけを公開する方法 ([Tool filtering](#tool-filtering))。
- サーバーが再利用可能なプロンプトも提供するかどうか ([Prompts](#prompts))。
- `list_tools()` をキャッシュすべきかどうか ([Caching](#caching))。
- MCP アクティビティがトレースにどう表示されるか ([Tracing](#tracing))。

ローカル MCP サーバー (`MCPServerStdio` 、 `MCPServerSse` 、 `MCPServerStreamableHttp`) では、承認ポリシーと呼び出しごとの `_meta` ペイロードも共通概念です。 Streamable HTTP セクションが最も完全なコード例を示しており、同じパターンが他のローカルトランスポートにも適用されます。

## 1. Hosted MCP server tools

Hosted ツールは、ツールの往復全体を OpenAI のインフラに委ねます。コード側でツールを列挙・呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーラベル（および任意のコネクタメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを列挙し、 Python プロセスへの追加コールバックなしで実行します。 Hosted ツールは現在、 Responses API の hosted MCP 統合をサポートする OpenAI モデルで動作します。

### 基本の Hosted MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して Hosted ツールを作成します。 `tool_config` 辞書は REST API に送る JSON を反映します:

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

Hosted サーバーはツールを自動公開するため、 `mcp_servers` に追加する必要はありません。

Hosted ツール検索で hosted MCP サーバーを遅延読み込みしたい場合は、 `tool_config["defer_loading"] = True` を設定し、エージェントに [`ToolSearchTool`][agents.tool.ToolSearchTool] を追加してください。これは OpenAI Responses モデルでのみサポートされます。完全なツール検索の設定と制約は [Tools](tools.md#hosted-tool-search) を参照してください。

### Hosted MCP 結果のストリーミング

Hosted ツールは、関数ツールとまったく同じ方法で結果のストリーミングをサポートします。 `Runner.run_streamed` を使うと、モデルがまだ処理中でも増分 MCP 出力を消費できます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機密操作を実行可能な場合、各ツール実行前に人手またはプログラムによる承認を要求できます。 `tool_config` の `require_approval` に、単一ポリシー (`"always"` 、 `"never"`) またはツール名からポリシーへの辞書を設定します。 Python 側で判断するには `on_approval_request` コールバックを提供します。

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

このコールバックは同期・非同期のどちらでもよく、モデルが実行継続のために承認データを必要とするたびに呼び出されます。

### コネクタをバックエンドとする Hosted サーバー

Hosted MCP は OpenAI コネクタもサポートします。 `server_url` を指定する代わりに、 `connector_id` とアクセストークンを渡します。 Responses API が認証を処理し、 hosted サーバーがコネクタのツールを公開します。

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

ストリーミング、承認、コネクタを含む完全動作する Hosted ツールのサンプルは、[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。 Streamable HTTP サーバーは、トランスポートを制御したい場合や、低遅延を保ちながら独自インフラ内でサーバーを実行したい場合に最適です。

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

コンストラクターは追加オプションを受け取ります:

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` はテキスト出力より `tool_result.structured_content` を優先するかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は `list_tools()` と `call_tool()` の自動リトライを追加します。
- `tool_filter` はツールの一部だけを公開できます（[Tool filtering](#tool-filtering) 参照）。
- `require_approval` はローカル MCP ツールで human-in-the-loop 承認ポリシーを有効化します。
- `failure_error_function` はモデルに見える MCP ツール失敗メッセージをカスタマイズします。代わりにエラーを送出したい場合は `None` を設定します。
- `tool_meta_resolver` は `call_tool()` 前に呼び出しごとの MCP `_meta` ペイロードを注入します。

### ローカル MCP サーバーの承認ポリシー

`MCPServerStdio` 、 `MCPServerSse` 、 `MCPServerStreamableHttp` はすべて `require_approval` を受け付けます。

サポートされる形式:

- すべてのツールに対する `"always"` または `"never"` 。
- `True` / `False` （ always/never と同等）。
- ツールごとのマップ。例: `{"delete_file": "always", "read_file": "never"}` 。
- グループ化オブジェクト:
  `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}` 。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

完全な一時停止/再開フローは、 [Human-in-the-loop](human_in_the_loop.md) と `examples/mcp/get_all_mcp_tools_example/main.py` を参照してください。

### `tool_meta_resolver` による呼び出しごとのメタデータ

MCP サーバーが `_meta` のリクエストメタデータ（例: テナント ID やトレースコンテキスト）を必要とする場合は `tool_meta_resolver` を使います。以下の例は、 `Runner.run(...)` に `context` として `dict` を渡すことを前提にしています。

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

実行コンテキストが Pydantic モデル、 dataclass 、またはカスタムクラスの場合は、代わりに属性アクセスでテナント ID を読み取ってください。

### MCP ツール出力: テキストと画像

MCP ツールが画像コンテンツを返す場合、 SDK はそれを自動的に画像ツール出力エントリにマップします。テキスト/画像混在レスポンスは出力項目のリストとして転送されるため、エージェントは通常の関数ツールからの画像出力と同じ方法で MCP 画像結果を処理できます。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨にしています。新規統合では Streamable HTTP または stdio を優先し、 SSE はレガシーサーバー用のみにしてください。

MCP サーバーが HTTP with SSE トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外の API は Streamable HTTP サーバーと同一です。

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

ローカルサブプロセスとして実行される MCP サーバーには、 [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使います。 SDK はプロセスを起動し、パイプを開いたまま維持し、コンテキストマネージャー終了時に自動で閉じます。このオプションは、素早い概念実証や、サーバーがコマンドラインエントリポイントしか公開していない場合に有用です。

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

複数の MCP サーバーがある場合は、 `MCPServerManager` を使って事前に接続し、接続済みサブセットをエージェントに公開します。コンストラクターオプションと再接続動作は [MCPServerManager API reference](ref/mcp/manager.md) を参照してください。

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

- `active_servers` は `drop_failed_servers=True` （デフォルト）時に接続成功したサーバーのみを含みます。
- 失敗は `failed_servers` と `errors` で追跡されます。
- 最初の接続失敗で例外を発生させるには `strict=True` を設定します。
- 失敗サーバーのみ再試行するには `reconnect(failed_only=True)` 、全サーバーを再起動するには `reconnect(failed_only=False)` を呼びます。
- ライフサイクル動作を調整するには `connect_timeout_seconds` 、 `cleanup_timeout_seconds` 、 `connect_in_parallel` を使います。

## 共通サーバー機能

以下のセクションは MCP サーバートランスポート全体に適用されます（正確な API 表面はサーバークラスに依存します）。

## Tool filtering

各 MCP サーバーはツールフィルターをサポートしており、エージェントに必要な関数だけを公開できます。フィルタリングは構築時または実行ごとに動的に行えます。

### 静的ツールフィルタリング

シンプルな許可/ブロックリストを設定するには [`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使います:

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

`allowed_tool_names` と `blocked_tool_names` の両方が与えられた場合、 SDK はまず許可リストを適用し、その残り集合からブロック対象ツールを除外します。

### 動的ツールフィルタリング

より高度なロジックには [`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る callable を渡します。 callable は同期・非同期のいずれでもよく、ツールを公開すべき場合に `True` を返します。

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

フィルターコンテキストは、アクティブな `run_context` 、ツールを要求する `agent` 、および `server_name` を公開します。

## Prompts

MCP サーバーは、エージェント指示を動的生成するプロンプトも提供できます。プロンプト対応サーバーは次の 2 つのメソッドを公開します:

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は具体的なプロンプトを取得します（必要に応じてパラメーター付き）。

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

各エージェント実行は各 MCP サーバーで `list_tools()` を呼びます。リモートサーバーは目立つレイテンシを生む可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。後で最新リストを強制したい場合は、サーバーインスタンスで `invalidate_tools_cache()` を呼びます。

## Tracing

[Tracing](./tracing.md) は、以下を含む MCP アクティビティを自動で記録します:

1. ツール一覧取得のための MCP サーバー呼び出し。
2. ツール呼び出し上の MCP 関連情報。

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)

## 参考情報

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio 、 SSE 、 Streamable HTTP サンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクタを含む完全な hosted MCP デモ。