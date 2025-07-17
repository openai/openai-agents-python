---
search:
  exclude: true
---
# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（通称  MCP ）は、ツールとコンテキストを  LLM  に提供するための方法です。 MCP  のドキュメントでは次のように説明されています:

> MCP is an open protocol that standardizes how applications provide context to LLMs. Think of MCP like a USB-C port for AI applications. Just as USB-C provides a standardized way to connect your devices to various peripherals and accessories, MCP provides a standardized way to connect AI models to different data sources and tools.

 Agents SDK  は  MCP  をサポートしています。これにより、さまざまな  MCP サーバーを使用してエージェントにツールやプロンプトを提供できます。

## MCP サーバー

現在、 MCP  仕様では利用するトランスポートメカニズムに基づき、3 種類のサーバーを定義しています:

1. **stdio** サーバー: アプリケーションのサブプロセスとして実行されます。ローカルで動いていると考えてください。  
2. **HTTP over SSE** サーバー: リモートで実行され、URL を介して接続します。  
3. **Streamable HTTP** サーバー: MCP 仕様で定義されている Streamable HTTP トランスポートを使用してリモートで実行されます。

これらのサーバーへは [`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスで接続できます。

たとえば、[公式 MCP ファイルシステムサーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) を使用する場合は次のようになります。

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

## MCP サーバーの利用

 MCP サーバーはエージェントに追加できます。 Agents SDK はエージェントの実行毎に MCP サーバーの `list_tools()` を呼び出し、 MCP サーバーのツールを  LLM  に認識させます。 LLM  が MCP サーバーのツールを呼び出すと、SDK はそのサーバーの `call_tool()` を実行します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールのフィルタリング

 MCP サーバーにツールフィルターを設定して、エージェントで利用可能なツールを制限できます。 SDK は静的および動的フィルタリングの両方をサポートしています。

### 静的なツールフィルタリング

単純な許可 / ブロックリストの場合は、静的フィルタリングを利用できます:

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

**`allowed_tool_names` と `blocked_tool_names` の両方を設定した場合、処理順序は以下のとおりです:**
1. まず `allowed_tool_names`（許可リスト）を適用し、指定されたツールのみを残します  
2. 次に `blocked_tool_names`（ブロックリスト）を適用し、残ったツールから指定されたツールを除外します  

たとえば `allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定した場合、使用できるツールは `read_file` と `write_file` だけになります。

### 動的なツールフィルタリング

より複雑なフィルタリングロジックには、関数を用いた動的フィルターを利用できます:

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

`ToolFilterContext` では以下へアクセスできます:
- `run_context`: 現在の実行コンテキスト  
- `agent`: ツールを要求しているエージェント  
- `server_name`: MCP サーバー名  

## プロンプト

 MCP サーバーはプロンプトも提供でき、パラメーター付きで動的にエージェントの instructions を生成できます。これにより再利用可能なインストラクションテンプレートを作成し、パラメーターでカスタマイズできます。

### プロンプトの使用

プロンプトをサポートする MCP サーバーは、次の 2 つの主要メソッドを提供します:

- `list_prompts()`: サーバー上の利用可能なプロンプトを列挙します  
- `get_prompt(name, arguments)`: 指定したプロンプトをオプションのパラメーター付きで取得します  

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

エージェントが実行されるたびに、 MCP サーバーの `list_tools()` が呼ばれます。サーバーがリモートの場合、これはレイテンシの原因になります。ツール一覧を自動でキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡します。ツール一覧が変更されないことが確実な場合のみ使用してください。

キャッシュを無効化したい場合は、サーバーの `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドのコード例

動作する完全なコード例は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) をご覧ください。

## トレーシング

[トレーシング](./tracing.md) は以下を含む MCP 操作を自動的にキャプチャします:

1. ツール一覧取得のための MCP サーバーへの呼び出し  
2. 関数呼び出しに関する MCP 関連情報  

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)