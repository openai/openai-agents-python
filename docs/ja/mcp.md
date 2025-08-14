---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（別名 MCP）は、LLM にツールとコンテキストを提供する方法です。MCP のドキュメントより:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP は AI アプリケーションのための USB-C ポートのようなものです。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続するための標準化された方法を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続するための標準化された方法を提供します。

Agents SDK は MCP をサポートします。これにより、幅広い MCP サーバーを使用して、エージェントにツールやプロンプトを提供できます。

## MCP servers

現在、MCP の仕様は使用するトランスポート方式に基づいて、3 種類のサーバーを定義しています:

1. **stdio** サーバーは、アプリケーションのサブプロセスとして実行されます。いわゆる「ローカル」で動作すると考えられます。
2. **HTTP over SSE** サーバーはリモートで動作します。URL を介して接続します。
3. **Streamable HTTP** サーバーは、MCP 仕様で定義された Streamable HTTP トランスポートを使用してリモートで動作します。

これらのサーバーには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスを使って接続できます。

例えば、[公式の MCP filesystem server](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) は次のように使います。

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

## Using MCP servers

MCP サーバーはエージェントに追加できます。Agents SDK は、エージェントが実行されるたびに MCP サーバー上で `list_tools()` を呼び出します。これにより、LLM は MCP サーバーのツールを把握できます。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーで `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## Tool filtering

MCP サーバーでツールフィルターを設定することで、エージェントで利用可能なツールを絞り込めます。SDK は静的および動的なツールフィルタリングの両方をサポートします。

### Static tool filtering

単純な許可 / ブロックリストには、静的フィルタリングを使用できます:

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

**`allowed_tool_names` と `blocked_tool_names` の両方が設定されている場合の処理順序は次のとおりです:**
1. まず `allowed_tool_names`（許可リスト）を適用し、指定したツールのみを残します
2. 次に `blocked_tool_names`（ブロックリスト）を適用し、残ったツールから指定したツールを除外します

例えば、`allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定すると、`read_file` と `write_file` のツールだけが利用可能になります。

### Dynamic tool filtering

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

`ToolFilterContext` では次にアクセスできます:
- `run_context`: 現在の実行コンテキスト
- `agent`: ツールを要求しているエージェント
- `server_name`: MCP サーバー名

## Prompts

MCP サーバーは、エージェントの instructions を動的に生成するために使用できるプロンプトも提供できます。これにより、パラメーターでカスタマイズ可能な再利用可能な instructions テンプレートを作成できます。

### Using prompts

プロンプトをサポートする MCP サーバーは、2 つの主要なメソッドを提供します:

- `list_prompts()`: サーバー上で利用可能なすべてのプロンプトを一覧表示します
- `get_prompt(name, arguments)`: 任意のパラメーター付きで特定のプロンプトを取得します

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

## Caching

エージェントが実行されるたびに、MCP サーバー上で `list_tools()` が呼び出されます。これは、サーバーがリモートサーバーの場合は特にレイテンシーの原因になります。ツール一覧を自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡します。ツール一覧が変更されないことが確実な場合にのみ行ってください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出せます。

## End-to-end examples

完全な動作する code examples は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) をご覧ください。

## Tracing

[トレーシング](./tracing.md) は MCP の操作を自動的に取得します。内容には次が含まれます:

1. ツール一覧の取得のための MCP サーバー呼び出し
2. 関数呼び出しに関する MCP 関連情報

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)