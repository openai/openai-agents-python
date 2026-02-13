---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (MCP) は、アプリケーションが言語モデルに対してツールやコンテキストを公開する方法を標準化します。公式ドキュメントより:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI
> applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP
> provides a standardized way to connect AI models to different data sources and tools.

Agents Python SDK は、複数の MCP トランスポートを理解します。これにより、既存の MCP サーバーを再利用したり、ファイルシステム、HTTP、またはコネクターがバックにあるツールを エージェント に公開するために独自のサーバーを構築したりできます。

## MCP 統合の選択

MCP サーバーを エージェント に組み込む前に、ツール呼び出しをどこで実行するか、そして到達できるトランスポートはどれかを決めます。以下のマトリクスは、Python SDK がサポートする選択肢を要約したものです。

| 必要なもの                                                                        | 推奨オプション                                    |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| OpenAI の Responses API により、モデルに代わって一般公開で到達可能な MCP サーバーを呼び出したい | [`HostedMCPTool`][agents.tool.HostedMCPTool] による **Hosted MCP server tools** |
| ローカルまたはリモートで実行している Streamable HTTP サーバーに接続したい                  | [`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] による **Streamable HTTP MCP servers** |
| Server-Sent Events による HTTP を実装したサーバーと通信したい                          | [`MCPServerSse`][agents.mcp.server.MCPServerSse] による **HTTP with SSE MCP servers** |
| ローカルプロセスを起動し、stdin/stdout 経由で通信したい                             | [`MCPServerStdio`][agents.mcp.server.MCPServerStdio] による **stdio MCP servers** |

以下のセクションでは、それぞれの選択肢、設定方法、そしてどのトランスポートを優先すべきかを説明します。

## エージェント レベルの MCP 設定

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

- `convert_schemas_to_strict` はベストエフォートです。スキーマを変換できない場合は、元のスキーマが使用されます。
- `failure_error_function` は、MCP ツール呼び出しの失敗がモデルにどのように提示されるかを制御します。
- `failure_error_function` が未設定の場合、SDK はデフォルトのツールエラー整形器を使用します。
- サーバー レベルの `failure_error_function` は、そのサーバーについて `Agent.mcp_config["failure_error_function"]` を上書きします。

## 1. Hosted MCP server tools

ホスト型ツールは、ツールの往復全体を OpenAI のインフラストラクチャに押し込みます。あなたのコードがツールを列挙して呼び出す代わりに、[`HostedMCPTool`][agents.tool.HostedMCPTool] がサーバー ラベル (および任意のコネクター メタデータ) を Responses API に転送します。モデルはリモート サーバーのツールを列挙し、Python プロセスへの追加のコールバックなしにそれらを呼び出します。ホスト型ツールは現在、Responses API のホスト型 MCP 統合をサポートする OpenAI モデルで動作します。

### 基本のホスト型 MCP ツール

エージェント の `tools` リストに [`HostedMCPTool`][agents.tool.HostedMCPTool] を追加して、ホスト型ツールを作成します。`tool_config` の dict は、REST API に送る JSON を反映します。

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

ホスト型サーバーは自動的にそのツールを公開します。`mcp_servers` に追加する必要はありません。

### ホスト型 MCP 結果のストリーミング

ホスト型ツールは、関数ツールとまったく同じ方法で結果の ストリーミング をサポートします。`Runner.run_streamed` を使って、モデルがまだ作業中の間に増分の MCP 出力を消費します。

```python
result = Runner.run_streamed(agent, "Summarise this repository's top languages")
async for event in result.stream_events():
    if event.type == "run_item_stream_event":
        print(f"Received: {event.item}")
print(result.final_output)
```

### 任意の承認フロー

サーバーが機微な操作を実行できる場合、各ツール実行の前に人手またはプログラムによる承認を要求できます。`tool_config` の `require_approval` を、単一のポリシー (`"always"`, `"never"`) またはツール名からポリシーへの dict として設定します。Python 内で判断するには、`on_approval_request` コールバックを提供します。

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

このコールバックは同期・非同期のどちらでもよく、モデルが実行を継続するために承認データを必要とするたびに呼び出されます。

### コネクターがバックにあるホスト型サーバー

ホスト型 MCP は OpenAI コネクターもサポートします。`server_url` を指定する代わりに `connector_id` とアクセストークンを提供します。Responses API が認証を処理し、ホスト型サーバーがコネクターのツールを公開します。

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

ネットワーク接続を自分で管理したい場合は、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] を使用します。Streamable HTTP サーバーは、トランスポートを制御したい場合や、レイテンシを低く保ったまま自社インフラ内でサーバーを実行したい場合に最適です。

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

- `client_session_timeout_seconds` は HTTP の読み取りタイムアウトを制御します。
- `use_structured_content` は、テキスト出力より `tool_result.structured_content` を優先するかを切り替えます。
- `max_retry_attempts` と `retry_backoff_seconds_base` は、`list_tools()` と `call_tool()` に自動リトライを追加します。
- `tool_filter` はツールのサブセットのみを公開できます ([Tool filtering](#tool-filtering) を参照)。
- `require_approval` はローカル MCP ツールに対する human-in-the-loop の承認ポリシーを有効化します。
- `failure_error_function` はモデルに見える MCP ツール失敗メッセージをカスタマイズします。代わりにエラーを送出するには `None` に設定します。
- `tool_meta_resolver` は、`call_tool()` の前に呼び出しごとの MCP `_meta` ペイロードを注入します。

### ローカル MCP サーバー向けの承認ポリシー

`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` はいずれも `require_approval` を受け取ります。

サポートされる形式:

- すべてのツールに対して `"always"` または `"never"`。
- `True` / `False` (always/never と同等)。
- ツールごとの map。例: `{"delete_file": "always", "read_file": "never"}`。
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

MCP サーバーが `_meta` 内のリクエスト メタデータ (たとえばテナント ID やトレース コンテキスト) を期待する場合は `tool_meta_resolver` を使用します。以下の例では、`Runner.run(...)` に `context` として `dict` を渡すことを想定しています。

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

実行コンテキストが Pydantic モデル、dataclass、またはカスタム クラスの場合は、代わりに属性アクセスでテナント ID を読み取ってください。

### MCP ツール出力: テキストと画像

MCP ツールが画像コンテンツを返す場合、SDK はそれを自動的に画像ツール出力エントリにマップします。テキスト/画像が混在するレスポンスは出力アイテムのリストとして転送されるため、エージェント は通常の関数ツールからの画像出力と同じ方法で MCP 画像結果を消費できます。

## 3. HTTP with SSE MCP servers

!!! warning

    MCP プロジェクトは Server-Sent Events トランスポートを非推奨にしました。新しい統合では Streamable HTTP または stdio を優先し、SSE はレガシー サーバーにのみ残してください。

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

ローカルのサブプロセスとして実行する MCP サーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] を使用します。SDK はプロセスを起動し、パイプを開いたままにし、コンテキスト マネージャーの終了時に自動的に閉じます。この選択肢は、素早い概念実証や、サーバーがコマンドラインのエントリポイントしか公開していない場合に便利です。

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

## 5. MCP サーバー マネージャー

複数の MCP サーバーがある場合は、`MCPServerManager` を使用して事前に接続し、接続済みのサブセットを エージェント に公開します。

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

- `drop_failed_servers=True` (デフォルト) の場合、`active_servers` には接続に成功したサーバーのみが含まれます。
- 失敗は `failed_servers` と `errors` で追跡されます。
- 最初の接続失敗で例外を投げるには `strict=True` を設定します。
- 失敗したサーバーをリトライするには `reconnect(failed_only=True)` を呼び出し、すべてのサーバーを再起動するには `reconnect(failed_only=False)` を呼び出します。
- ライフサイクル挙動を調整するには `connect_timeout_seconds`、`cleanup_timeout_seconds`、`connect_in_parallel` を使用します。

## Tool filtering

各 MCP サーバーはツールフィルターをサポートしており、エージェント が必要とする関数だけを公開できます。フィルタリングは構築時に行うことも、実行ごとに動的に行うこともできます。

### 静的ツールフィルタリング

単純な allow/block リストを設定するには、[`create_static_tool_filter`][agents.mcp.create_static_tool_filter] を使用します。

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

`allowed_tool_names` と `blocked_tool_names` の両方が指定された場合、SDK はまず allow リストを適用し、その後、残った集合から block されたツールを削除します。

### 動的ツールフィルタリング

より複雑なロジックには、[`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取る callable を渡します。この callable は同期・非同期どちらでもよく、ツールを公開すべき場合に `True` を返します。

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

MCP サーバーは、エージェント の instructions を動的に生成するプロンプトも提供できます。プロンプトをサポートするサーバーは 2 つのメソッドを公開します。

- `list_prompts()` は利用可能なプロンプト テンプレートを列挙します。
- `get_prompt(name, arguments)` は具体的なプロンプトを取得し、必要に応じてパラメーターを渡せます。

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

エージェント の各実行では、各 MCP サーバーに対して `list_tools()` を呼び出します。リモート サーバーは目立つレイテンシを生む可能性があるため、すべての MCP サーバー クラスは `cache_tools_list` オプションを公開しています。ツール定義が頻繁に変わらないと確信できる場合にのみ `True` に設定してください。後で新しいリストを強制するには、サーバー インスタンスで `invalidate_tools_cache()` を呼び出します。

## Tracing

[Tracing](./tracing.md) は、以下を含む MCP アクティビティを自動的にキャプチャします。

1. ツール一覧を取得するための MCP サーバーへの呼び出し。
2. ツール呼び出しにおける MCP 関連情報。

![MCP トレーシング スクリーンショット](../assets/images/mcp-tracing.jpg)

## Further reading

- [Model Context Protocol](https://modelcontextprotocol.io/) – 仕様および設計ガイド。
- [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) – 実行可能な stdio、SSE、Streamable HTTP のサンプル。
- [examples/hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) – 承認とコネクターを含む、ホスト型 MCP の完全なデモ。