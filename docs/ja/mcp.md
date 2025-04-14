# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（通称 MCP）は、 LLM にツールやコンテキストを提供するための方法です。MCP ドキュメントからの引用です：

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP を AI アプリケーションのための USB-C ポートのようなものと考えてください。USB-C がさまざまな周辺機器やアクセサリにデバイスを接続する標準的な方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続する標準的な方法を提供します。

Agents SDK は MCP をサポートしています。これにより、幅広い MCP サーバーを利用して、エージェントにツールを提供することができます。

## MCP サーバー

現在、MCP 仕様では、使用するトランスポートメカニズムに基づいて 2 種類のサーバーが定義されています：

1. **stdio** サーバーは、アプリケーションのサブプロセスとして実行されます。ローカルで実行されるものと考えることができます。
2. **HTTP over SSE** サーバーはリモートで実行されます。URL を介して接続します。

これらのサーバーに接続するには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] および [`MCPServerSse`][agents.mcp.server.MCPServerSse] クラスを使用できます。

例えば、[公式 MCP ファイルシステムサーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) を使用する場合は、次のようになります。

```python
async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    }
) as server:
    tools = await server.list_tools()
```

## MCP サーバーの利用

MCP サーバーはエージェントに追加できます。Agents SDK は、エージェントが実行されるたびに MCP サーバーの `list_tools()` を呼び出します。これにより、LLM は MCP サーバーのツールを認識できるようになります。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーの `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## キャッシュ

エージェントが実行されるたびに、MCP サーバーの `list_tools()` が呼び出されます。特にサーバーがリモートの場合、これはレイテンシの原因となることがあります。ツールリストを自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] および [`MCPServerSse`][agents.mcp.server.MCPServerSse] の両方に `cache_tools_list=True` を渡すことができます。ツールリストが変更されないことが確実な場合のみ、この設定を行ってください。

キャッシュを無効化したい場合は、サーバーで `invalidate_tools_cache()` を呼び出すことができます。

## エンドツーエンドの code examples

[examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) で、完全な動作 code examples をご覧いただけます。

## トレーシング

[トレーシング](../tracing.md) は、以下を含む MCP の操作を自動的に記録します：

1. MCP サーバーへのツールリスト取得の呼び出し
2. 関数呼び出しに関する MCP 関連情報

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)