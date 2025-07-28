---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（通称 MCP）は、LLM へツールとコンテキストを提供するための仕組みです。MCP ドキュメントからの引用です。

> MCP は、アプリケーションが LLM へコンテキストを提供する方法を標準化するオープンプロトコルです。MCP を AI アプリケーション向けの USB-C ポートと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに標準的な方法で接続できるようにするのと同じく、MCP は AI モデルを異なるデータソースやツールに標準的な方法で接続できるようにします。

Agents SDK は MCP をサポートしています。これにより、幅広い MCP サーバーを利用してエージェントへツールやプロンプトを提供できます。

## MCP サーバー

現在、MCP 仕様は使用するトランスポートメカニズムに基づき、次の 3 種類のサーバーを定義しています。

1. **stdio** サーバー: アプリケーションのサブプロセスとして実行され、ローカルで動作するイメージです。  
2. **HTTP over SSE** サーバー: リモートで実行され、URL で接続します。  
3. **Streamable HTTP** サーバー: MCP 仕様で定義された Streamable HTTP トランスポートを使用してリモートで実行されます。  

これらのサーバーへは `MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` クラスを用いて接続できます。

たとえば、公式 MCP ファイルシステムサーバーを使用する場合は次のようになります。

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

MCP サーバーはエージェントに追加できます。Agents SDK はエージェント実行時に毎回 MCP サーバーへ `list_tools()` を呼び出し、LLM に MCP サーバーのツールを認識させます。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーへ `call_tool()` を実行します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールのフィルタリング

MCP サーバーでツールフィルターを設定することで、エージェントが利用できるツールを制御できます。SDK は静的および動的なツールフィルタリングの両方をサポートしています。

### 静的ツールフィルタリング

単純な許可 / ブロックリストの場合は静的フィルタリングを使用できます。

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

**`allowed_tool_names` と `blocked_tool_names` の両方が設定されている場合、処理順序は次のとおりです。**  
1. まず `allowed_tool_names`（許可リスト）を適用し、指定されたツールだけを残します。  
2. 次に `blocked_tool_names`（ブロックリスト）を適用し、残ったツールから指定されたツールを除外します。  

例: `allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定すると、利用可能なのは `read_file` と `write_file` のみになります。

### 動的ツールフィルタリング

より複雑なフィルタリングロジックが必要な場合は、関数を使った動的フィルターを利用できます。

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

`ToolFilterContext` でアクセスできる情報  
- `run_context`: 現在のランコンテキスト  
- `agent`: ツールを要求しているエージェント  
- `server_name`: MCP サーバー名  

## プロンプト

MCP サーバーはプロンプトも提供でき、これを使ってエージェントの instructions を動的に生成できます。これにより、パラメーターでカスタマイズ可能な再利用可能な instructions テンプレートを作成できます。

### プロンプトの使用

プロンプトをサポートする MCP サーバーは、次の 2 つの主要メソッドを提供します。

- `list_prompts()`: サーバー上で利用可能なすべてのプロンプトを一覧表示します。  
- `get_prompt(name, arguments)`: オプションのパラメーター付きで特定のプロンプトを取得します。  

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

エージェントが実行されるたびに、MCP サーバーへ `list_tools()` を呼び出します。サーバーがリモートの場合、これはレイテンシの原因になります。ツール一覧を自動的にキャッシュするには、`MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp` に `cache_tools_list=True` を渡します。ツール一覧が変更されないことが確実な場合のみ使用してください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドのコード例

動作する完全なコード例は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) をご覧ください。

## トレーシング

[トレーシング](./tracing.md) は MCP 操作を自動的に取得します。対象は次のとおりです。

1. MCP サーバーへのツール一覧取得呼び出し  
2. 関数呼び出しに関する MCP 情報  

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)