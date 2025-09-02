---
search:
  exclude: true
---
# Model context protocol (MCP)

The [Model context protocol](https://modelcontextprotocol.io/introduction) (aka MCP) is a way to provide tools and context to the LLM. From the MCP docs:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP provides a standardized way to connect AI models to different data sources and tools.

Agents SDK は MCP をサポートしています。これにより、幅広い MCP サーバーを使用して、エージェント にツールやプロンプトを提供できます。

## MCP サーバー

現在、MCP の仕様は、使用するトランスポート方式に基づいて 3 種類の サーバー を定義しています。

1. **stdio** サーバーはアプリケーションのサブプロセスとして実行されます。いわゆる「ローカル」で動作すると考えられます。
2. **HTTP over SSE** サーバーはリモートで実行されます。URL 経由で接続します。
3. **Streamable HTTP** サーバーは、MCP 仕様で定義された Streamable HTTP トランスポートを使用してリモートで実行されます。

これらの サーバー に接続するには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスを使用できます。

たとえば、[official MCP filesystem server](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) は次のように使用します。

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

## MCP サーバーの使用

MCP サーバーは エージェント に追加できます。Agents SDK は、エージェント の実行ごとに MCP サーバー上で `list_tools()` を呼び出します。これにより、LLM は MCP サーバーのツールを認識します。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーで `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールのフィルタリング

MCP サーバーでツールフィルターを設定することで、エージェント で利用可能なツールを制限できます。SDK は静的フィルタリングと動的フィルタリングの両方をサポートします。

### 静的ツールフィルタリング

単純な許可/ブロック リストには、静的フィルタリングを使用できます。

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

**`allowed_tool_names` と `blocked_tool_names` の両方が設定されている場合の処理順序は次のとおりです。**
1. まず `allowed_tool_names`（許可リスト）を適用し、指定したツールのみを残します
2. 次に `blocked_tool_names`（ブロックリスト）を適用し、残ったツールから指定したツールを除外します

たとえば、`allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定した場合、`read_file` と `write_file` のツールのみが利用可能になります。

### 動的ツールフィルタリング

より複雑なフィルタリングロジックには、関数を用いた動的フィルターを使用できます。

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

`ToolFilterContext` では次の情報にアクセスできます。
- `run_context`: 現在の実行コンテキスト
- `agent`: ツールを要求している エージェント
- `server_name`: MCP サーバーの名前

## プロンプト

MCP サーバーは、エージェント の指示を動的に生成するために使用できるプロンプトも提供できます。これにより、パラメーターでカスタマイズ可能な再利用可能な指示テンプレートを作成できます。

### プロンプトの使用

プロンプトをサポートする MCP サーバーは、次の 2 つの主要なメソッドを提供します。

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

## キャッシュ

エージェント が実行されるたびに、MCP サーバーで `list_tools()` が呼び出されます。特に サーバー がリモートの場合はレイテンシが発生し得ます。ツール一覧を自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡します。ツール一覧が変更されないと確信できる場合にのみ使用してください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドの code examples

[examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) で、完全に動作する code examples を参照してください。

## トレーシング

[Tracing](./tracing.md) は、以下を含む MCP の操作を自動的に取得します。

1. ツール一覧の取得に対する MCP サーバーへの呼び出し
2. 関数呼び出しに関する MCP 関連情報

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)