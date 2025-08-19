---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（aka MCP）は、LLM にツールとコンテキストを提供するための方法です。MCP のドキュメントより:

> MCP は、アプリケーションが LLMs にコンテキストを提供する方法を標準化するオープンなプロトコルです。MCP は AI アプリケーション向けの USB-C ポートのようなものだと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続する標準化された方法を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続する標準化された方法を提供します。

Agents SDK は MCP をサポートしています。これにより、幅広い MCPサーバー を使用して、エージェント にツールやプロンプトを提供できます。

## MCPサーバー

現在、MCP の仕様は使用するトランスポートメカニズムに基づいて 3 種類のサーバーを定義しています:

1. **stdio** サーバーはアプリケーションのサブプロセスとして実行されます。ローカルで動作していると捉えることができます。
2. **HTTP over SSE** サーバーはリモートで実行され、URL で接続します。
3. **Streamable HTTP** サーバーは、MCP 仕様で定義された Streamable HTTP トランスポートを使ってリモートで実行されます。

これらのサーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスで接続できます。

例えば、[公式 MCP filesystem サーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem)は次のように使います。

```python
from agents.run_context import RunContextWrapper

async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    }
) as server:
    # Note: In practice, you typically add the server to an Agent
    # and let the framework handle tool listing automatically.
    # Direct calls to list_tools() require run_context and agent parameters.
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test", instructions="test")
    tools = await server.list_tools(run_context, agent)
```

## MCPサーバーの使用

MCPサーバー は エージェント に追加できます。Agents SDK は エージェント が実行されるたびに MCPサーバー 上で `list_tools()` を呼び出します。これにより、LLM は MCPサーバー のツールを認識します。LLM が MCPサーバー のツールを呼び出すと、SDK はそのサーバーで `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールのフィルタリング

MCPサーバー 上でツールフィルターを設定することで、エージェント が利用できるツールを絞り込めます。SDK は静的および動的なツールフィルタリングの両方をサポートしています。

### 静的ツールフィルタリング

単純な許可/ブロックリストには、静的フィルタリングを使用できます:

```python
from agents.mcp import create_static_tool_filter

# Only expose specific tools from this server
server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=create_static_tool_filter(
        allowed_tool_names=["read_file", "write_file"]
    )
)

# Exclude specific tools from this server
server = MCPServerStdio(
    params={
        "command": "npx", 
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=create_static_tool_filter(
        blocked_tool_names=["delete_file"]
    )
)

```

**`allowed_tool_names` と `blocked_tool_names` が両方設定されている場合の処理順序は次のとおりです:**
1. まず `allowed_tool_names`（許可リスト）を適用 — 指定したツールのみを残します
2. 次に `blocked_tool_names`（ブロックリスト）を適用 — 残った中から指定したツールを除外します

例えば、`allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定した場合、`read_file` と `write_file` のみが利用可能になります。

### 動的ツールフィルタリング

より複雑なフィルタリングロジックには、関数を使った動的フィルターを使用できます:

```python
from agents.mcp import ToolFilterContext

# Simple synchronous filter
def custom_filter(context: ToolFilterContext, tool) -> bool:
    """Example of a custom tool filter."""
    # Filter logic based on tool name patterns
    return tool.name.startswith("allowed_prefix")

# Context-aware filter
def context_aware_filter(context: ToolFilterContext, tool) -> bool:
    """Filter tools based on context information."""
    # Access agent information
    agent_name = context.agent.name

    # Access server information  
    server_name = context.server_name

    # Implement your custom filtering logic here
    return some_filtering_logic(agent_name, server_name, tool)

# Asynchronous filter
async def async_filter(context: ToolFilterContext, tool) -> bool:
    """Example of an asynchronous filter."""
    # Perform async operations if needed
    result = await some_async_check(context, tool)
    return result

server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
    tool_filter=custom_filter  # or context_aware_filter or async_filter
)
```

`ToolFilterContext` では次の情報にアクセスできます:
- `run_context`: 現在の実行コンテキスト
- `agent`: ツールを要求している エージェント
- `server_name`: MCPサーバー の名称

## プロンプト

MCPサーバー は、エージェント の instructions を動的に生成するためのプロンプトも提供できます。これにより、パラメーター でカスタマイズ可能な再利用可能なインストラクションテンプレートを作成できます。

### プロンプトの使用

プロンプトをサポートする MCPサーバー は次の 2 つの主要なメソッドを提供します:

- `list_prompts()`: サーバー上で利用可能なすべてのプロンプトを一覧表示
- `get_prompt(name, arguments)`: 任意のパラメーター 付きで特定のプロンプトを取得

```python
# List available prompts
prompts_result = await server.list_prompts()
for prompt in prompts_result.prompts:
    print(f"Prompt: {prompt.name} - {prompt.description}")

# Get a specific prompt with parameters
prompt_result = await server.get_prompt(
    "generate_code_review_instructions",
    {"focus": "security vulnerabilities", "language": "python"}
)
instructions = prompt_result.messages[0].content.text

# Use the prompt-generated instructions with an Agent
agent = Agent(
    name="Code Reviewer",
    instructions=instructions,  # Instructions from MCP prompt
    mcp_servers=[server]
)
```

## キャッシュ

エージェント が実行されるたびに、MCPサーバー 上で `list_tools()` が呼び出されます。特にサーバーがリモート サーバー の場合、これはレイテンシーを増加させる可能性があります。ツール一覧を自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡します。ツール一覧が変更されないことが確実な場合にのみ行ってください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出せます。

## エンドツーエンドの code examples

動作する完全な code examples は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) をご覧ください。

## トレーシング

[トレーシング](./tracing.md) は、次を含む MCP の操作を自動的に取得します:

1. ツール一覧取得のための MCPサーバー への呼び出し
2. 関数呼び出しに関連する MCP の情報

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)