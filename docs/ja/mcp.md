# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction) (別名 MCP) は、LLM にツールとコンテキストを提供する方法です。MCP ドキュメントからの引用:

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンプロトコルです。MCP を AI アプリケーションのための USB-C ポートのように考えてください。USB-C がデバイスをさまざまな周辺機器やアクセサリーに接続する標準化された方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続する標準化された方法を提供します。

エージェント SDK は MCP をサポートしています。これにより、エージェントにツールを提供するために幅広い MCP サーバーを使用できます。

## MCP サーバー

現在、MCP 仕様は使用するトランスポートメカニズムに基づいて2種類のサーバーを定義しています:

1. **stdio** サーバーは、アプリケーションのサブプロセスとして実行されます。これらは「ローカルで」実行されると考えることができます。
2. **HTTP over SSE** サーバーはリモートで実行されます。URL を介して接続します。

これらのサーバーに接続するには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] と [`MCPServerSse`][agents.mcp.server.MCPServerSse] クラスを使用できます。

例えば、[公式 MCP ファイルシステムサーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) を使用する方法は次のとおりです。

```python
async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    }
) as server:
    tools = await server.list_tools()
```

## MCP サーバーの使用

MCP サーバーはエージェントに追加できます。エージェント SDK は、エージェントが実行されるたびに MCP サーバーで `list_tools()` を呼び出します。これにより、LLM は MCP サーバーのツールを認識します。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーで `call_tool()` を呼び出します。

```python

agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## キャッシング

エージェントが実行されるたびに、MCP サーバーで `list_tools()` を呼び出します。これは、特にサーバーがリモートサーバーの場合、レイテンシーの影響を受ける可能性があります。ツールのリストを自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] と [`MCPServerSse`][agents.mcp.server.MCPServerSse] に `cache_tools_list=True` を渡すことができます。ツールリストが変更されないことが確実な場合にのみこれを行うべきです。

キャッシュを無効にしたい場合は、サーバーで `invalidate_tools_cache()` を呼び出すことができます。

## エンドツーエンドの例

[examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) で完全な動作例を確認してください。

## トレーシング

[トレーシング](./tracing.md) は、MCP 操作を自動的にキャプチャします。これには以下が含まれます:

1. ツールをリストするための MCP サーバーへの呼び出し
2. 関数呼び出しに関する MCP 関連情報

![MCP Tracing Screenshot](../assets/images/mcp-tracing.jpg)