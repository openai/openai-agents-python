---
search:
  exclude: true
---
# モデルコンテキストプロトコル (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (別名 MCP) は、LLM にツールとコンテキストを提供するための手段です。MCP ドキュメントより引用します。

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP を AI アプリケーション向けの USB-C ポートのように考えてください。USB-C がデバイスとさまざまな周辺機器やアクセサリを接続する標準化された方法を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続する標準化された方法を提供します。

Agents SDK は MCP をサポートしており、幅広い MCP サーバーを利用して エージェント にツールやプロンプトを提供できます。

## MCP サーバー

現在、MCP 仕様では使用するトランスポートメカニズムに基づいて 3 種類のサーバーを定義しています。

1. **stdio** サーバー: アプリケーションのサブプロセスとして実行されます。ローカルで動作すると考えられます。
2. **HTTP over SSE** サーバー: リモートで実行され、URL 経由で接続します。
3. **Streamable HTTP** サーバー: MCP 仕様で定義された Streamable HTTP トランスポートを用いてリモートで実行されます。

これらのサーバーには [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスを使って接続できます。

たとえば、[公式 MCP filesystem サーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) を使用する場合は次のようになります。

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

MCP サーバーは エージェント に追加できます。Agents SDK は エージェント が実行されるたびに MCP サーバーの `list_tools()` を呼び出し、LLM に MCP サーバーのツールを認識させます。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーの `call_tool()` を実行します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールフィルタリング

MCP サーバーでツールフィルターを設定することで、エージェント が利用できるツールを制御できます。SDK は静的および動的フィルタリングの両方をサポートします。

### 静的ツールフィルタリング

単純な許可 / ブロックリストの場合は、静的フィルタリングを使用します。

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

**`allowed_tool_names` と `blocked_tool_names` の両方を設定した場合の処理順序は次のとおりです。**  
1. まず `allowed_tool_names` (許可リスト) を適用し、指定したツールだけを残します。  
2. 次に `blocked_tool_names` (ブロックリスト) を適用し、残ったツールから指定したツールを除外します。  

たとえば `allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定すると、利用可能なのは `read_file` と `write_file` だけになります。

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

`ToolFilterContext` では次の情報を取得できます。  
- `run_context`: 現在の実行コンテキスト  
- `agent`: ツールを要求しているエージェント  
- `server_name`: MCP サーバー名  

## プロンプト

MCP サーバーは、エージェント の instructions を動的に生成するためのプロンプトも提供できます。これにより、パラメーターでカスタマイズ可能な再利用可能な instruction テンプレートを作成できます。

### プロンプトの使用

プロンプトをサポートする MCP サーバーは、次の 2 つの主要メソッドを提供します。

- `list_prompts()`: サーバーで利用可能なプロンプトを一覧表示します  
- `get_prompt(name, arguments)`: オプションのパラメーター付きで特定のプロンプトを取得します  

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

エージェント が実行されるたびに MCP サーバーの `list_tools()` が呼び出されるため、サーバーがリモートの場合はレイテンシーが発生する可能性があります。[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡すと、ツール一覧を自動的にキャッシュできます。ツール一覧が変わらないことが確実な場合にのみ設定してください。

キャッシュを無効化したい場合は、サーバーの `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドのコード例

動作する完全なコード例は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) を参照してください。

## トレーシング

[Tracing](./tracing.md) では以下を自動的にキャプチャします。

1. ツール一覧を取得する MCP サーバーへの呼び出し  
2. 関数呼び出しに関する MCP 関連情報  

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)