---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールや
コンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は、AI
> アプリケーションにおける USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリに接続する標準化された方法を提供するのと同様に、MCP
> は AI モデルをさまざまなデータソースやツールに接続する標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートに対応しています。これにより、既存の MCP サーバーを再利用したり、独自に構築して、ファイルシステム、HTTP、またはコネクターをバックエンドとするツールをエージェントに公開できます。

## MCP 統合の選択

MCP サーバーをエージェントに組み込む前に、ツール呼び出しをどこで実行すべきか、どのトランスポートに到達できるかを決めてください。次の表は、Python SDK がサポートする選択肢の概要です。

| 必要なこと                                                                        | 推奨オプション                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API に、モデルに代わって公開到達可能な MCP サーバーを呼び出させる| **ホスト型 MCP サーバーツール** （[`HostedMCPTool`][agents.tool.HostedMCPTool] 経由） |
| ローカルまたはリモートで実行している Streamable HTTP サーバーに接続する                  | **Streamable HTTP MCP サーバー** （[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 経由） |
| Server-Sent Events を用いた HTTP を実装しているサーバーと通信する                          | **SSE を用いた HTTP MCP サーバー** （[`MCPServerSse`][agents.mcp.server.MCPServerSse] 経由） |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                             | **stdio MCP サーバー** （[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 経由） |

以降のセクションでは、各オプション、その設定方法、あるトランスポートを別のトランスポートより優先すべきタイミングについて説明します。

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
        # Prefix local MCP tool names with their server name.
        "include_server_in_tool_names": True,
    },
)
```

注:

- `convert_schemas_to_strict` はベストエフォートです。スキーマを変換できない場合は、元のスキーマが使用されます。
- `failure_error_function` は、MCP ツール呼び出しの失敗をモデルにどのように提示するかを制御します。
- `failure_error_function` が未設定の場合、SDK はデフォルトのツールエラーフォーマッターを使用します。
- サーバーレベルの `failure_error_function` は、そのサーバーについて `Agent.mcp_config["failure_error_function"]` を上書きします。
- `include_server_in_tool_names` はオプトインです。有効にすると、各ローカル MCP ツールは、決定論的なサーバー接頭辞付きの名前でモデルに公開されます。これにより、複数の MCP サーバーが同じ名前のツールを公開する場合の衝突を避けやすくなります。生成される名前は ASCII セーフで、関数ツール名の長さ制限内に収まり、同じエージェント上の既存のローカル関数ツール名および有効化されたハンドオフ名を避けます。それでも SDK は元のサーバー上で元の MCP ツール名を呼び出します。

## トランスポート共通のパターン

トランスポートを選択した後、多くの統合では同じ追加判断が必要になります。

- ツールのサブセットのみを公開する方法（[ツールフィルタリング](#tool-filtering)）。
- サーバーが再利用可能なプロンプトも提供するかどうか（[プロンプト](#prompts)）。
- `list_tools()` をキャッシュすべきかどうか（[キャッシュ](#caching)）。
- MCP アクティビティがトレースにどのように表示されるか（[トレーシング](#tracing)）。

ローカル MCP サーバー（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`）では、承認ポリシーと呼び出しごとの `_meta` ペイロードも共通の概念です。Streamable HTTP セクションでは最も完全な例を示しており、同じパターンは他のローカルトランスポートにも適用されます。

## 1. ホスト型 MCP サーバーツール

ホスト型ツールでは、ツールのラウンドトリップ全体を OpenAI のインフラに委ねます。ツールの一覧取得と呼び出しをコード側で行う代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーラベル（および任意のコネクターメタデータ）を Responses API に転送します。モデルはリモートサーバーのツールを一覧表示し、Python プロセスへの追加のコールバックなしにそれらを呼び出します。現在、ホスト型ツールは Responses API のホスト型 MCP 統合をサポートする OpenAI モデルで動作します。

### 基本的なホスト型 MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加してホスト型ツールを作成します。`tool_config` 辞書は REST API に送信する JSON と同じ構造です:

```python
import asyncio

from agents import Agent, HostedMCPTool, Runner

async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Use the DeepWiki hosted MCP server to inspect openai/openai-agents-python.",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "deepwiki",
                    "server_url": "https://mcp.deepwiki.com/mcp",
                    "require_approval": "never",
                }
            )
        ],
    )

    result = await Runner.run(
        agent,
        "Which language is the repository openai/openai-agents-python written in?",
    )
    print(result.final_output)

asyncio.run(main())
```

ホスト型サーバーはツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

ホスト型ツール検索にホスト型 MCP サーバーを遅延読み込みさせたい場合は、`tool_config["defer_loading"] = True` を設定し、エージェントに [`ToolSearchTool`][agents.tool.ToolSearchTool] を追加します。これは OpenAI Responses モデルでのみサポートされます。ツール検索の完全な設定と制約については、[ツール](tools.md#hosted-tool-search) を参照してください。

### ホスト型 MCP の結果のストリーミング

ホスト型ツールは、関数ツールとまったく同じ方法で結果のストリーミングをサポートします。モデルがまだ処理中でも、`Runner.run_streamed` を使用して
増分的な MCP 出力を受け取れます:

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機密性の高い操作を実行できる場合、各ツール実行の前に人間またはプログラムによる承認を必須にできます。`tool_config` で `require_approval` を、単一のポリシー（`"always"`、`"never"`）またはツール名をポリシーにマッピングする辞書として設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

```python
from agents import MCPToolApprovalFunctionResult, MCPToolApprovalRequest

SAFE_TOOLS = {"read_wiki_structure", "read_wiki_contents", "ask_question"}

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
                "server_label": "deepwiki",
                "server_url": "https://mcp.deepwiki.com/mcp",
                "require_approval": "always",
            },
            on_approval_request=approve_tool,
        )
    ],
)
```

このコールバックは同期または非同期にでき、モデルが実行を継続するための承認データを必要とするたびに呼び出されます。

### コネクター対応のホスト型サーバー

ホスト型 MCP は OpenAI コネクターにも対応しています。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト型サーバーがコネクターのツールを公開します。

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

完全に動作するホスト型ツールのサンプル（ストリーミング、承認、コネクターを含む）は [`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP サーバー

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御したい場合や、レイテンシを低く保ちながら自分のインフラ内でサーバーを実行したい場合に最適です。

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
- `use_structured_content` は、`tool_result.structured_content` をテキスト出力より優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` は、ツールのサブセットのみを公開できるようにします（[ツールフィルタリング](#tool-filtering) を参照）。
- `require_approval` は、ローカル MCP ツールでヒューマンインザループの承認ポリシーを有効にします。
- `failure_error_function` は、モデルに表示される MCP ツール失敗メッセージをカスタマイズします。`None` に設定すると、代わりにエラーを送出します。
- `tool_meta_resolver` は、呼び出しごとの MCP `_meta` ペイロードを `call_tool()` の前に注入します。

### ローカル MCP サーバーの承認ポリシー

`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` はいずれも `require_approval` を受け取ります。

サポートされる形式:

- すべてのツールに対する `"always"` または `"never"`。
- `True` / `False`（always/never と同等）。
- ツールごとのマップ。例: `{"delete_file": "always", "read_file": "never"}`。
- グループ化されたオブジェクト: `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

完全な一時停止 / 再開フローについては、[ヒューマンインザループ](human_in_the_loop.md) と `examples/mcp/get_all_mcp_tools_example/main.py` を参照してください。

### `tool_meta_resolver` による呼び出しごとのメタデータ

MCP サーバーが `_meta` にリクエストメタデータ（たとえばテナント ID やトレースコンテキスト）を期待する場合は、`tool_meta_resolver` を使用します。下の例では、`Runner.run(...)` に `context` として `dict` を渡すことを前提としています。

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

実行コンテキストが Pydantic モデル、データクラス、またはカスタムクラスの場合は、属性アクセスでテナント ID を読み取ってください。

### MCP ツール出力: テキストと画像

MCP ツールが画像コンテンツを返すと、SDK はそれを画像ツール出力エントリーに自動的にマッピングします。テキスト / 画像の混在レスポンスは出力項目のリストとして転送されるため、エージェントは通常の関数ツールからの画像出力を扱うのと同じ方法で MCP の画像結果を扱えます。

## 3. SSE を用いた HTTP MCP サーバー

!!! warning

    MCP プロジェクトでは Server-Sent Events トランスポートが非推奨になりました。新しい統合では Streamable HTTP または stdio を優先し、SSE はレガシーサーバーにのみ使用してください。

MCP サーバーが SSE を用いた HTTP トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポート以外は、API は Streamable HTTP サーバーと同一です。

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

ローカルサブプロセスとして実行される MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたままにし、コンテキストマネージャーを抜けると自動的に閉じます。このオプションは、簡単な概念実証や、サーバーがコマンドラインエントリーポイントのみを公開する場合に役立ちます。

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

複数の MCP サーバーがある場合は、`MCPServerManager` を使用して事前に接続し、接続済みのサブセットをエージェントに公開します。コンストラクターオプションと再接続の動作については、[MCPServerManager API リファレンス](ref/mcp/manager.md) を参照してください。

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

主な動作:

- `active_servers` には、`drop_failed_servers=True`（デフォルト）の場合、正常に接続されたサーバーのみが含まれます。
- 失敗は `failed_servers` と `errors` で追跡されます。
- `strict=True` を設定すると、最初の接続失敗時にエラーを送出します。
- `reconnect(failed_only=True)` を呼び出すと失敗したサーバーを再試行し、`reconnect(failed_only=False)` を呼び出すとすべてのサーバーを再起動します。
- `connect_timeout_seconds`、`cleanup_timeout_seconds`、`connect_in_parallel` を使用してライフサイクル動作を調整します。

## 共通のサーバー機能

以下のセクションは MCP サーバートランスポート全体に適用されます（正確な API の範囲はサーバークラスによって異なります）。

## ツールフィルタリング

各 MCP サーバーはツールフィルターに対応しているため、エージェントに必要な関数だけを公開できます。フィルタリングは構築時に行うことも、実行ごとに動的に行うこともできます。

### 静的なツールフィルタリング

[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用して、シンプルな許可 / ブロックリストを設定します:

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後、残ったセットからブロックされたツールを削除します。

### 動的なツールフィルタリング

より高度なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取るコール可能オブジェクトを渡します。このコール可能オブジェクトは同期または非同期にでき、ツールを公開すべき場合に `True` を返します。

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

MCP サーバーは、エージェントの指示を動的に生成するプロンプトも提供できます。プロンプトに対応するサーバーは 2 つの
メソッドを公開します:

- `list_prompts()` は利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は具体的なプロンプトを取得します。任意でパラメーターを指定できます。

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

エージェントを実行するたびに、各 MCP サーバーで `list_tools()` が呼び出されます。リモートサーバーでは目に見えるレイテンシが発生する可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変更されないと確信できる場合にのみ、`True` に設定してください。後で最新のリストを強制的に取得するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[トレーシング](./tracing.md) は、次を含む MCP アクティビティを自動的にキャプチャします。

1. ツールを一覧表示するための MCP サーバーへの呼び出し。
2. ツール呼び出し上の MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## 参考情報

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクターを含む、完全なホスト型 MCP デモ。