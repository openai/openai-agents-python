---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（MCP）は、アプリケーションがツールやコンテキストを言語モデルに公開する方法を標準化します。公式ドキュメントでは、次のように説明されています。

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は、AI
> アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続する標準化された方法を提供するのと同様に、MCP
> は AI モデルをさまざまなデータソースやツールに接続する標準化された方法を提供します。

Agents Python SDK は、複数の MCP トランスポートを認識します。これにより、既存の MCP サーバーを再利用したり、ファイルシステム、HTTP、またはコネクターを基盤とするツールをエージェントに公開する独自の MCP サーバーを構築したりできます。

!!! warning "接続前の MCP サーバーの信頼性確認"

    MCP ツールは、モデルコンテキストのデータを公開し、提供された認証情報を使用して操作を実行できます。信頼できるサーバーのみに接続し、最小権限の認証情報を使用してください。また、アクセストークンは URL ではなく認証フィールドまたはヘッダーに保持し、機密性の高い操作には承認を必須としてください。[OpenAI の MCP セキュリティガイダンス](https://developers.openai.com/api/docs/guides/tools-connectors-mcp#risks-and-safety)も参照してください。

## MCP 統合の選択 {#choosing-an-mcp-integration}

MCP サーバーをエージェントに接続する前に、ツール呼び出しをどこで実行するか、またどのトランスポートにアクセスできるかを決めます。以下の表は、Python SDK がサポートするオプションをまとめたものです。

| 必要なこと                                                                           | 推奨オプション                                          |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API が、モデルに代わって一般公開された MCP サーバーを呼び出す    | [`HostedMCPTool`][agents.tool.HostedMCPTool] による **ホスト型 MCP サーバーツール** |
| ローカルまたはリモートで実行する Streamable HTTP サーバーに接続する                  | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] による **Streamable HTTP MCP サーバー** |
| Server-Sent Events を使用する HTTP を実装したサーバーと通信する                      | [`MCPServerSse`][agents.mcp.server.MCPServerSse] による **SSE 対応 HTTP MCP サーバー** |
| ローカルプロセスを起動し、stdin/stdout 経由で通信する                                | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] による **stdio MCP サーバー** |

以下のセクションでは、各オプション、その設定方法、および各トランスポートを選択すべき状況について説明します。

## MCP Python SDK v1 と v2 {#mcp-python-sdk-v1-and-v2}

Agents SDK は、依存関係の範囲 `mcp>=1.19.0,<3` を通じて、`mcp` Python パッケージの両方のメジャーバージョンをサポートします。インストールされている `mcp` パッケージのバージョンは、サーバーとの間でネゴシエートされる MCP プロトコルバージョンとは別です。Agents SDK は、インストールされているパッケージのメジャーバージョンを検出し、stdio、SSE、および Streamable HTTP 接続を自動的に調整するため、通常のサーバー設定ではバージョンを切り替える必要はありません。

MCP Python SDK v2 がインストールされている場合、Agents SDK は設定されたローカルトランスポートを `mode="auto"` でラップして、v2 の `mcp.Client` を作成します。クライアントはまず、インストールされている MCP SDK がサポートする最新のプロトコルバージョンで `server/discover` プローブを送信します。最新のサーバーはこのプローブに応答し、クライアントはその結果を採用します。古いサーバーが `server/discover` をサポートしていない場合、クライアントは従来の `initialize` ハンドシェイクにフォールバックし、そこでネゴシエートされたプロトコルバージョンを使用します。したがって、MCP Python SDK v2 をインストールしても、すべての接続で最新の MCP プロトコルバージョンが強制的に使用されるわけではありません。MCP Python SDK の[プロトコルバージョンネゴシエーションガイド](https://py.sdk.modelcontextprotocol.io/protocol-versions/)を参照してください。

ほとんどのアプリケーションでは、依存関係リゾルバーに互換性のあるバージョンを選択させることを推奨します。アプリケーションを特定のメジャーバージョンに固定する必要がある場合は、`openai-agents` とともに明示的な制約を追加します。

```bash
# MCP Python SDK v1
pip install "mcp>=1.19.0,<2"

# MCP Python SDK v2
pip install "mcp>=2,<3"
```

HTTP トランスポートのカスタマイズでは、インストールされている MCP パッケージが所有する HTTP スタックを使用する必要があります。

| カスタマイズ | MCP Python SDK v1 | MCP Python SDK v2 |
| --- | --- | --- |
| `params["auth"]` | `httpx.Auth` | `httpx2.Auth` |
| `params["httpx_client_factory"]` の戻り値 | `httpx.AsyncClient` | `httpx2.AsyncClient` |
| `MCPServerStreamableHttp` `params["ignore_initialized_notification_failure"] = True` | サポート対象 | サポート対象外。接続前に拒否されます |

可能な場合は、以下の Streamable HTTP の例に示すように、`Authorization` ヘッダーを使用してください。`Authorization` ヘッダーは、どちらのパッケージバージョンでも変更せずに使用できます。アプリケーションが `params["auth"]` または `params["httpx_client_factory"]` を指定する場合、それらの値には、インストールされている `mcp` パッケージのメジャーバージョンに対応する HTTP 型を使用する必要があります。アプリケーションが `MCPServerStreamableHttp` の `params["ignore_initialized_notification_failure"] = True` を設定する場合、アップグレード前に `mcp<2` を維持するか、そのオプションを無効にする必要があります。

これらのローカルな `mcp` の依存関係要件は、リモート MCP 接続を OpenAI Responses API が管理するため、[`HostedMCPTool`][agents.tool.HostedMCPTool] には適用されません。

## エージェントレベルの MCP 設定 {#agent-level-mcp-configuration}

トランスポートの選択に加えて、`Agent.mcp_config` を設定することで、MCP ツールの準備方法を調整できます。

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

注記：

- `convert_schemas_to_strict` はベストエフォートです。スキーマを変換できない場合は、元のスキーマが使用されます。
- `failure_error_function` は、MCP ツール呼び出しの失敗をモデルにどのように提示するかを制御します。
- `failure_error_function` が設定されていない場合、SDK はデフォルトのツールエラーフォーマッターを使用します。
- サーバーレベルの `failure_error_function` は、そのサーバーについて `Agent.mcp_config["failure_error_function"]` を上書きします。
- `include_server_in_tool_names` はオプトインです。有効にすると、各ローカル MCP ツールは、決定論的なサーバープレフィックス付きの名前でモデルに公開されます。これは、複数の MCP サーバーが同名のツールを公開する場合の衝突回避に役立ちます。生成される名前は ASCII セーフで、`FunctionTool` インスタンスの名前の長さ制限内に収まり、同じエージェントに設定されたローカル `FunctionTool` インスタンスの名前や、有効なハンドオフの名前とは衝突しません。SDK は引き続き、元のサーバー上で元の MCP ツール名を使用して呼び出します。

## トランスポート間の共通パターン {#shared-patterns-across-transports}

トランスポートを選択した後、ほとんどの統合では、次の事項について判断する必要があります。

- ツールの一部のみを公開する方法（[ツールフィルタリング](#tool-filtering)）。
- サーバーが再利用可能なプロンプトも提供するかどうか（[プロンプト](#prompts)）。
- `list_tools()` をキャッシュするかどうか（[キャッシュ](#caching)）。
- MCP アクティビティがトレースにどのように表示されるか（[トレーシング](#tracing)）。

ローカル MCP サーバー（`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`）では、承認ポリシーと呼び出しごとの `_meta` ペイロードも共通の概念です。Streamable HTTP のセクションでは最も完全なコード例を示しており、同じパターンを他のローカルトランスポートにも適用できます。

## 1. ホスト型 MCP サーバーツール {#1-hosted-mcp-server-tools}

ホスト型ツールでは、ツールのラウンドトリップ全体が OpenAI のインフラストラクチャ内で実行されます。コード側でツールを一覧表示して呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバーラベルと、必要に応じてコネクターのメタデータを Responses API に転送します。モデルは、Python プロセスへの追加のコールバックを行わずに、リモートサーバーのツールを一覧表示して呼び出します。現在、ホスト型ツールは、Responses API のホスト型 MCP 統合をサポートする OpenAI モデルで動作します。

### 基本的なホスト型 MCP ツール {#basic-hosted-mcp-tool}

エージェントの `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型ツールを作成します。`tool_config`
の辞書は、REST API に送信する JSON と同じ構造です。

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

ホスト型サーバーは、そのツールを自動的に公開します。`mcp_servers` に追加する必要はありません。

ホスト型ツール検索によってホスト型 MCP サーバーを遅延読み込みする場合は、`tool_config["defer_loading"] = True` を設定し、[`ToolSearchTool`][agents.tool.ToolSearchTool] をエージェントに追加します。これは OpenAI Responses モデルでのみサポートされます。ツール検索の完全な設定と制約については、[ツール](tools.md#hosted-tool-search)を参照してください。

### ホスト型 MCP の実行結果のストリーミング {#streaming-hosted-mcp-results}

ホスト型ツールでは、関数ツールとまったく同じ方法で実行結果のストリーミングがサポートされます。モデルが処理中の間に、`Runner.run_streamed` を使用して
MCP の増分出力を受け取ります。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### オプションの承認フロー {#optional-approval-flows}

サーバーが機密性の高い操作を実行できる場合、各ツールの実行前に人間またはプログラムによる承認を必須にできます。`tool_config` 内の `require_approval` に、単一のポリシー（`"always"`、`"never"`）またはツール名をポリシーにマッピングする辞書を設定します。Python 内で判断するには、`on_approval_request` コールバックを指定します。

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

### コネクターを基盤とするホスト型サーバー {#connector-backed-hosted-servers}

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

ストリーミング、承認、コネクターを含む、完全に動作するホスト型ツールのサンプルは、[`examples/hosted_mcp`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)にあります。

## 2. Streamable HTTP MCP サーバー {#2-streamable-http-mcp-servers}

ネットワーク接続を自身で管理する場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御する場合や、低レイテンシーを維持しながら自身のインフラストラクチャ内でサーバーを実行する場合に最適です。

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

コンストラクターは、次の追加オプションを受け入れます。

- `client_session_timeout_seconds` は、MCP ClientSession の読み取りタイムアウトを制御します。`datetime.timedelta` で表現できる正の有限値で、かつ 1 マイクロ秒以上の値を指定すると、有限のタイムアウトが設定されます。`None` と `0` を指定すると無効になります。それ以外の値は、サーバーの構築時に拒否されます。
- `use_structured_content` は、テキスト出力より `tool_result.structured_content` を優先するかどうかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に対する自動再試行を追加します。
- `tool_filter` を使用すると、ツールの一部のみを公開できます（[ツールフィルタリング](#tool-filtering)を参照）。
- `require_approval` は、ローカル MCP ツールで Human-in-the-loop の承認ポリシーを有効にします。
- `failure_error_function` は、モデルに表示される MCP ツールの失敗メッセージをカスタマイズします。代わりにエラーを発生させるには、`None` に設定します。
- `tool_meta_resolver` は、`call_tool()` の前に、呼び出しごとの MCP `_meta` ペイロードを挿入します。

### ローカル MCP サーバーの承認ポリシー {#approval-policies-for-local-mcp-servers}

`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` は、いずれも `require_approval` を受け入れます。

サポートされる形式：

- すべてのツールに対する `"always"` または `"never"`。
- `True` ではすべてのツールに承認が必要で、`False` ではどのツールにも承認は不要です（それぞれ `"always"` および `"never"` と同等です）。
- ツールごとのマップ。例：`{"delete_file": "always", "read_file": "never"}`。
- グループ化されたオブジェクト：`{"always": {"tool_names": [...]}, "never": {"tool_names": [...]}}`。

```python
async with MCPServerStreamableHttp(
    name="Filesystem MCP",
    params={"url": "http://localhost:8000/mcp"},
    require_approval={"always": {"tool_names": ["delete_file"]}},
) as server:
    ...
```

一時停止と再開を含む完全なフローについては、[Human-in-the-loop](human_in_the_loop.md)および `examples/mcp/get_all_mcp_tools_example/main.py` を参照してください。

### `tool_meta_resolver` による呼び出しごとのメタデータ {#per-call-metadata-with-tool_meta_resolver}

MCP サーバーが `_meta` 内にリクエストメタデータ（テナント ID やトレースコンテキストなど）を必要とする場合は、`tool_meta_resolver` を使用します。以下の例では、`dict` を `context` として `Runner.run(...)` に渡すことを前提としています。

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

実行コンテキストが Pydantic モデル、dataclass、またはカスタムクラスの場合は、属性アクセスを使用してテナント ID を読み取ります。

### MCP ツールの出力：テキスト、画像、その他のコンテンツ {#mcp-tool-outputs-text-images-and-other-content}

MCP の実行結果でコンテンツブロックが使用されている場合、SDK はテキストコンテンツをテキスト出力として転送し、画像コンテンツをツール出力内の画像型エントリーにマッピングします。音声ブロックやリソースブロックを含むその他の MCP コンテンツブロック型については、SDK は、そのブロックを有効な JSON としてシリアライズした値を持つテキスト出力を転送します。複数のコンテンツブロックを含むレスポンスは、出力項目のリストとして転送されます。`use_structured_content=True` が、空でなくエラーでもない `structuredContent` ペイロードを選択した場合、その構造化ペイロードがこれらのコンテンツブロックより優先されます。構造化コンテンツが存在しないか空の場合は、コンテンツブロックにフォールバックします。

## 3. SSE 対応 HTTP MCP サーバー {#3-http-with-sse-mcp-servers}

!!! warning

    MCP プロジェクトでは、Server-Sent Events トランスポートは非推奨になっています。新しい統合では Streamable HTTP または stdio を優先し、SSE は従来のサーバーにのみ使用してください。

MCP サーバーが SSE 対応 HTTP トランスポートを実装している場合は、[`MCPServerSse`][agents.mcp.server.MCPServerSse] をインスタンス化します。トランスポートを除き、API は Streamable HTTP サーバーと同一です。

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

## 4. stdio MCP サーバー {#4-stdio-mcp-servers}

ローカルサブプロセスとして実行される MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを生成し、パイプを開いたまま維持し、コンテキストマネージャーの終了時に自動的に閉じます。このオプションは、簡単な概念実証や、サーバーがコマンドラインのエントリーポイントのみを公開する場合に役立ちます。

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

## 5. MCP サーバーマネージャー {#5-mcp-server-manager}

複数の MCP サーバーがある場合は、`MCPServerManager` を使用して事前に接続し、正常に接続されたサーバーのみをエージェントに公開します。コンストラクターのオプションと再接続の動作については、[MCPServerManager API リファレンス](ref/mcp/manager.md)を参照してください。

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

主な動作：

- `drop_failed_servers=True` の場合（デフォルト）、`active_servers` には正常に接続されたサーバーのみが含まれます。
- 失敗は `failed_servers` と `errors` で追跡されます。
- 最初の接続失敗時に例外を発生させるには、`strict=True` を設定します。
- 失敗したサーバーを再試行するには `reconnect(failed_only=True)` を、すべてのサーバーを再起動するには `reconnect(failed_only=False)` を呼び出します。
- `connect_all()`、`reconnect()`、`cleanup_all()` の呼び出しは直列化されます。あるライフサイクル操作がすでに実行中の場合、別のライフサイクル操作は、同じサーバーへの接続やクリーンアップを同時に行わず、その操作が完了するまで待機します。
- ライフサイクルの動作を調整するには、`connect_timeout_seconds`、`cleanup_timeout_seconds`、`connect_in_parallel` を設定します。どちらのライフサイクルタイムアウトもデフォルトは 10 秒です。正の有限秒、または無効にするための `None` を受け入れ、構築時と代入時の両方で検証されます。即時の期限が設定されてしまうため、0 は拒否されます。

## サーバーに共通する機能 {#common-server-capabilities}

以下のセクションは、MCP サーバーの各トランスポートに共通して適用されます（具体的な API サーフェスはサーバークラスによって異なります）。

## ツールフィルタリング {#tool-filtering}

各 MCP サーバーはツールフィルターをサポートしているため、エージェントが必要とする関数のみを公開できます。フィルタリングは、構築時に静的に行うことも、実行ごとに動的に行うこともできます。

### 静的ツールフィルタリング {#static-tool-filtering}

単純な許可リストとブロックリストを設定するには、[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK は最初に許可リストを適用し、その後、残ったツールからブロック対象のツールを削除します。

### 動的ツールフィルタリング {#dynamic-tool-filtering}

より複雑なロジックでは、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る callable を渡します。callable は同期または非同期にでき、ツールを公開する場合は `True` を返します。

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

フィルターコンテキストからは、アクティブな `run_context`、ツールを要求している `agent`、および `server_name` にアクセスできます。

## プロンプト {#prompts}

MCP サーバーは、エージェントへの指示を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは、次の 2 つの
メソッドを公開します。

- `list_prompts()` は、利用可能なプロンプトテンプレートを列挙します。
- `get_prompt(name, arguments)` は、必要に応じてパラメーターを指定して、具体的なプロンプトを取得します。

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

## ページネーション {#pagination}

組み込みのローカル MCP サーバークラスは、ツールとプロンプトを一覧表示する際に `nextCursor` を自動的にたどります。`list_tools()` は、フィルターの適用またはキャッシュへの格納前に完全なツール一覧を収集し、`list_prompts()` は `nextCursor=None` を含む 1 つの統合された実行結果を返します。後続のページが失敗した場合や、サーバーが同じカーソルを繰り返した場合、部分的な実行結果を公開またはキャッシュする代わりに、操作はエラーを発生させます。

リソースは引き続き明示的にページ分割されます。次のページを取得するには、`list_resources()` または `list_resource_templates()` から取得した `nextCursor` を、`cursor` 引数として再度渡します。

## キャッシュ {#caching}

エージェントを実行するたびに、各 MCP サーバー上で `list_tools()` が呼び出されます。リモートサーバーでは顕著なレイテンシーが生じる可能性があるため、すべての MCP サーバークラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変更されないと確信できる場合にのみ、`True` に設定してください。後で最新の一覧を強制的に取得するには、サーバーインスタンス上で `invalidate_tools_cache()` を呼び出します。

## トレーシング {#tracing}

[トレーシング](./tracing.md)では、次の項目を含む MCP アクティビティが自動的に記録されます。

1. ツールを一覧表示するための MCP サーバーへの呼び出し。
2. ツール呼び出しに関する MCP 関連情報。

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)

## 関連資料 {#further-reading}

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認やコネクターを含む、ホスト型 MCP の完全なデモ。