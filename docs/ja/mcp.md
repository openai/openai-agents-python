---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションがツールとコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は AI
> アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリに接続するための標準化された方法を提供するのと同じように、MCP
> は AI モデルをさまざまなデータソースやツールに接続するための標準化された方法を提供します。

Agents Python SDK は複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、ファイルシステム、HTTP、またはコネクターに基づくツールをエージェントに公開するために独自に構築したりできます。

## MCP 連携の選択

MCP サーバーをエージェントに接続する前に、ツール呼び出しをどこで実行すべきか、どのトランスポートに到達できるかを決定します。以下の表は、Python SDK がサポートするオプションをまとめたものです。

| 必要なこと                                                                        | 推奨オプション                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API が、モデルに代わって公開到達可能な MCP サーバーを呼び出せるようにする| [`HostedMCPTool`][agents.tool.HostedMCPTool] 経由の **Hosted MCP server tools** |
| ローカルまたはリモートで実行している Streamable HTTP サーバーに接続する                  | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] 経由の **Streamable HTTP MCP servers** |
| Server-Sent Events を使用した HTTP を実装するサーバーと通信する                          | [`MCPServerSse`][agents.mcp.server.MCPServerSse] 経由の **HTTP with SSE MCP servers** |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                             | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] 経由の **stdio MCP servers** |

以下のセクションでは、各オプション、設定方法、あるトランスポートを別のものより優先すべき場合について説明します。

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
- `include_server_in_tool_names` はオプトインです。有効にすると、各ローカル MCP ツールは決定的なサーバー接頭辞付きの名前でモデルに公開されます。これにより、複数の MCP サーバーが同じ名前のツールを公開している場合の衝突を避けやすくなります。生成される名前は ASCII セーフで、関数ツール名の長さ制限内に収まり、同じエージェント上の既存のローカル関数ツール名や有効なハンドオフ名を避けます。SDK は引き続き、元のサーバー上で元の MCP ツール名を呼び出します。

## トランスポート間で共通するパターン

トランスポートを選択した後、多くの連携では同じ追加判断が必要になります。

- ツールの一部だけを公開する方法（[ツールフィルタリング](#tool-filtering)）。
- サーバーが再利用可能なプロンプトも提供するかどうか（[プロンプト](#prompts)）。
- `list_tools()` をキャッシュすべきかどうか（[キャッシュ](#caching)）。
- MCP アクティビティがトレースにどのように表示されるか（[トレーシング](#tracing)）。

ローカル MCP サーバー（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`）では、承認ポリシーと呼び出しごとの `_meta` ペイロードも共通概念です。Streamable HTTP セクションでは最も完全な例を示しており、同じパターンは他のローカルトランスポートにも適用されます。

## 1. Hosted MCP server tools

ホスト型ツールは、ツールの往復全体を OpenAI のインフラストラクチャに移します。コードがツールを一覧表示して呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーラベル（および任意のコネクターメタデータ）を Responses API に転送します。モデルは、Python プロセスへの追加コールバックなしでリモートサーバーのツールを一覧表示し、それらを呼び出します。ホスト型ツールは現在、Responses API のホスト型 MCP 連携をサポートする OpenAI モデルで動作します。

### 基本的な hosted MCP ツール

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型ツールを作成します。`tool_config`
dict は、REST API に送信する JSON を反映します。

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

ホスト型ツール検索でホスト型 MCP サーバーを遅延読み込みしたい場合は、`tool_config["defer_loading"] = True` を設定し、[`ToolSearchTool`][agents.tool.ToolSearchTool] をエージェントに追加します。これは OpenAI Responses モデルでのみサポートされます。完全なツール検索の設定と制約については、[ツール](tools.md#hosted-tool-search) を参照してください。

### hosted MCP 実行結果のストリーミング

ホスト型ツールは、関数ツールとまったく同じ方法で実行結果のストリーミングをサポートします。モデルがまだ動作している間に増分 MCP 出力を消費するには、`Runner.run_streamed` を使用します。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機密性の高い操作を実行できる場合、各ツール実行前に人間またはプログラムによる承認を要求できます。`tool_config` の `require_approval` に、単一のポリシー（`"always"`、`"never"`）またはツール名からポリシーへの dict のいずれかを設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

コールバックは同期または非同期にでき、モデルが実行を継続するために承認データを必要とするたびに呼び出されます。

### コネクターに基づく hosted サーバー

ホスト型 MCP は OpenAI コネクターもサポートします。`server_url` を指定する代わりに、`connector_id` とアクセストークンを指定します。Responses API が認証を処理し、ホスト型サーバーがコネクターのツールを公開します。

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

ストリーミング、承認、コネクターを含む、完全に動作するホスト型ツールのサンプルは
[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) にあります。

## 2. Streamable HTTP MCP servers

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを自分で制御する場合や、低レイテンシを維持しながら自分のインフラストラクチャ内でサーバーを実行したい場合に最適です。

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
- `use_structured_content` は、テキスト出力よりも `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` は、ツールの一部だけを公開できるようにします（[ツールフィルタリング](#tool-filtering) を参照）。
- `require_approval` は、ローカル MCP ツールに対して human-in-the-loop の承認ポリシーを有効にします。
- `failure_error_function` は、モデルに見える MCP ツール失敗メッセージをカスタマイズします。代わりにエラーを発生させるには、`None` に設定します。
- `tool_meta_resolver` は、`call_tool()` の前に呼び出しごとの MCP `_meta` ペイロードを注入します。

### ローカル MCP サーバーの承認ポリシー

`MCPServerStdio`、`MCPServerSse`、および `MCPServerStreamableHttp` はすべて `require_approval` を受け取ります。

サポートされる形式:

- すべてのツールに対する `"always"` または `"never"`。
- `True` / `False`（always/never と同等）。
- ツールごとのマップ。例: `{"delete_file": "always", "read_file": "never"}`。
- グループ化されたオブジェクト:
  `{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

完全な一時停止/再開フローについては、[Human-in-the-loop](human_in_the_loop.md) と `examples/mcp/get_all_mcp_tools_example/main.py` を参照してください。

### `tool_meta_resolver` による呼び出しごとのメタデータ

MCP サーバーが `_meta` にリクエストメタデータ（たとえばテナント ID やトレースコンテキスト）を期待する場合は、`tool_meta_resolver` を使用します。以下の例では、`Runner.run(...)` に `context` として `dict` を渡すことを想定しています。

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

実行コンテキストが Pydantic モデル、dataclass、またはカスタムクラスの場合は、代わりに属性アクセスでテナント ID を読み取ります。

### MCP ツール出力: テキストと画像

MCP ツールが画像コンテンツを返す場合、SDK はそれを画像ツール出力エントリに自動的にマッピングします。テキストと画像が混在したレスポンスは、出力項目のリストとして転送されるため、エージェントは通常の関数ツールからの画像出力を消費するのと同じ方法で MCP 画像実行結果を消費できます。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨にしました。新しい連携では Streamable HTTP または stdio を優先し、SSE はレガシーサーバーにのみ維持してください。

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

ローカルサブプロセスとして実行される MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたままにし、コンテキストマネージャーが終了すると自動的に閉じます。このオプションは、簡単な概念実証や、サーバーがコマンドラインエントリーポイントのみを公開している場合に役立ちます。

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
- 最初の接続失敗で例外を発生させるには、`strict=True` を設定します。
- 失敗したサーバーを再試行するには `reconnect(failed_only=True)` を、すべてのサーバーを再起動するには `reconnect(failed_only=False)` を呼び出します。
- ライフサイクル動作を調整するには、`connect_timeout_seconds`、`cleanup_timeout_seconds`、および `connect_in_parallel` を使用します。

## 共通のサーバー機能

以下のセクションは、MCP サーバートランスポート全体に適用されます（正確な API サーフェスはサーバークラスに依存します）。

## ツールフィルタリング

各 MCP サーバーはツールフィルターをサポートしているため、エージェントが必要とする関数だけを公開できます。フィルタリングは構築時、または実行ごとに動的に行えます。

### 静的ツールフィルタリング

単純な許可/ブロックリストを設定するには、[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず許可リストを適用し、その後、残りのセットからブロックされたツールを削除します。

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

MCP サーバーは、エージェントの instructions を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは、2 つのメソッドを公開します。

- `list_prompts()` は、利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じてパラメーター付きの具体的なプロンプトを取得します。

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

各エージェント実行では、各 MCP サーバーで `list_tools()` が呼び出されます。リモートサーバーは目に見えるレイテンシをもたらす可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変更されないと確信できる場合にのみ、`True` に設定してください。後で新しいリストを強制するには、サーバーインスタンスで `invalidate_tools_cache()` を呼び出します。

## トレーシング

[トレーシング](../tracing.md) は、以下を含む MCP アクティビティを自動的にキャプチャします。

1. ツールを一覧表示するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## 参考情報

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様と設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP サンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクターを含む完全なホスト型 MCP デモ。
