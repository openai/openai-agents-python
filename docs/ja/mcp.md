# Model context protocol (MCP)

[Model context protocol](https://modelcontextprotocol.io/introduction)（別名 MCP）は、LLM にツールやコンテキストを提供するための方法です。MCP のドキュメントから引用します：

> MCP は、アプリケーションが LLM にコンテキストを提供する方法を標準化するオープンなプロトコルです。MCP は AI アプリケーションにおける USB-C ポートのようなものです。USB-C がデバイスをさまざまな周辺機器やアクセサリに接続するための標準化された方法を提供するのと同様に、MCP は AI モデルをさまざまなデータソースやツールに接続するための標準化された方法を提供します。

Agents SDK は MCP をサポートしています。これにより、さまざまな MCP サーバーを使用して、エージェントにツールを提供できます。

## MCP サーバー

現在、MCP の仕様では、使用するトランスポートメカニズムに基づいて 2 種類のサーバーが定義されています：

1. **stdio** サーバーはアプリケーションのサブプロセスとして実行されます。これらは「ローカル」で実行されると考えることができます。
2. **HTTP over SSE** サーバーはリモートで実行されます。これらには URL を介して接続します。

これらのサーバーに接続するには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] および [`MCPServerSse`][agents.mcp.server.MCPServerSse] クラスを使用できます。

例えば、[公式 MCP ファイルシステムサーバー](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem)を使用する場合は以下のようになります：

```python
async with MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    }
) as server:
    tools = await server.list_tools()
```

## MCP サーバーの使用方法

MCP サーバーはエージェントに追加できます。Agents SDK はエージェントが実行されるたびに MCP サーバーの `list_tools()` を呼び出します。これにより、LLM は MCP サーバーのツールを認識します。LLM が MCP サーバーのツールを呼び出すと、SDK はそのサーバーの `call_tool()` を呼び出します。

```python
agent=Agent(
    name="Assistant",
    instructions="Use the tools to achieve the task",
    mcp_servers=[mcp_server_1, mcp_server_2]
)
```

## キャッシュ

エージェントが実行されるたびに、MCP サーバーの `list_tools()` が呼び出されます。特にサーバーがリモートの場合、これはレイテンシの原因となる可能性があります。ツールのリストを自動的にキャッシュするには、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio] と [`MCPServerSse`][agents.mcp.server.MCPServerSse] の両方に `cache_tools_list=True` を渡します。これは、ツールのリストが変更されないことが確実な場合にのみ行ってください。

キャッシュを無効化したい場合は、サーバーの `invalidate_tools_cache()` を呼び出します。

## エンドツーエンドのコード例

完全な動作コード例については、[examples/mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp) を参照してください。

## トレーシング

[トレーシング](./tracing.md) は、以下を含む MCP 操作を自動的にキャプチャします：

1. MCP サーバーへのツール一覧取得の呼び出し
2. 関数呼び出しに関する MCP 関連情報

![MCP トレーシングのスクリーンショット](../assets/images/mcp-tracing.jpg)