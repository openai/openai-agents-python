---
search:
  exclude: true
---
# モデルコンテキストプロトコル (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（別名 MCP）は、LLM にツールとコンテキストを提供する方法です。MCP ドキュメントより :

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP を AI アプリケーション向けの USB-C ポートと考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続する標準化された手段を提供するのと同様に、MCP は AI モデルを異なるデータソースやツールに接続するための標準化された手段を提供します。

Agents SDK は MCP をサポートしています。これにより、幅広い MCP サーバーを利用してエージェントにツールやプロンプトを提供できます。

## MCP サーバー

現在、MCP の仕様では使用するトランスポートメカニズムに基づいて 3 種類のサーバーが定義されています :

1. **stdio** サーバーはアプリケーションのサブプロセスとして実行されます。ローカルで動作していると考えてください。  
2. **HTTP over SSE** サーバーはリモートで実行されます。URL 経由で接続します。  
3. **Streamable HTTP** サーバーは、MCP 仕様で定義されている Streamable HTTP トランスポートを使用してリモートで実行されます。  

これらのサーバーへ接続するには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] クラスを使用します。

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

## MCP サーバーの使用

MCP サーバーはエージェントに追加できます。Agents SDK はエージェントの実行ごとに MCP サーバーへ `list_tools()` を呼び出します。これによって LLM は MCP サーバーのツールを認識できます。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーの `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## ツールフィルタリング

MCP サーバーでツールフィルターを設定することで、エージェントが利用できるツールを制限できます。SDK は静的および動的の両方のツールフィルタリングをサポートしています。

### 静的ツールフィルタリング

単純な許可 / ブロックリストの場合、静的フィルタリングを使用できます :

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

**`allowed_tool_names` と `blocked_tool_names` の両方を設定した場合の処理順序:**  
1. まず `allowed_tool_names`（許可リスト）を適用し、指定したツールだけを残します。  
2. 次に `blocked_tool_names`（ブロックリスト）を適用し、残ったツールから指定したツールを除外します。  

たとえば `allowed_tool_names=["read_file", "write_file", "delete_file"]` と `blocked_tool_names=["delete_file"]` を設定すると、`read_file` と `write_file` だけが利用可能になります。

### 動的ツールフィルタリング

より複雑なフィルタリング ロジックには、関数を使った動的フィルターを利用できます :

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

`ToolFilterContext` からは次の情報にアクセスできます :  
- `run_context` : 現在の実行コンテキスト  
- `agent` : ツールを要求しているエージェント  
- `server_name` : MCP サーバー名  

## プロンプト

MCP サーバーはプロンプトも提供でき、これを使ってエージェントの instructions を動的に生成できます。再利用可能な instructions テンプレートをパラメーター付きでカスタマイズできます。

### プロンプトの使用

プロンプトをサポートする MCP サーバーは以下の 2 つの主要メソッドを提供します :

- `list_prompts()` : サーバー上で利用可能なプロンプトを列挙  
- `get_prompt(name, arguments)` : 指定した名前のプロンプトをパラメーター付きで取得  

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

エージェントが実行されるたびに、MCP サーバーへ `list_tools()` が呼ばれます。サーバーがリモートの場合、これはレイテンシーの原因になります。ツール一覧を自動でキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] に `cache_tools_list=True` を渡してください。ツールリストが変わらないと確信できる場合のみ行ってください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドのコード例

完全な動作例は [examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) を参照してください。

## トレーシング

[トレーシング](./tracing.md) では MCP 操作が自動的に記録されます :

1. MCP サーバーへのツール一覧取得呼び出し  
2. 関数呼び出しに関する MCP 関連情報  

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)