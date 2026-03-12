---
search:
  exclude: true
---
# リリースプロセス/変更履歴

このプロジェクトは、`0.Y.Z` 形式を使ったセマンティックバージョニングの軽微な改変版に従っています。先頭の `0` は、 SDK が依然として急速に進化していることを示します。各コンポーネントは次のように増分します。

## マイナー (`Y`) バージョン

ベータとしてマークされていない公開インターフェースに対する **breaking changes** については、マイナーバージョン `Y` を増分します。たとえば、`0.0.x` から `0.1.x` への変更には breaking changes が含まれる可能性があります。

breaking changes を望まない場合は、プロジェクトで `0.0.x` バージョンに固定することを推奨します。

## パッチ (`Z`) バージョン

`Z` は non-breaking changes に対して増分します。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## Breaking change 変更履歴

### 0.12.0

このマイナーリリースでは **breaking change** は導入されていません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0) を確認してください。

### 0.11.0

このマイナーリリースでは **breaking change** は導入されていません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0) を確認してください。

### 0.10.0

このマイナーリリースでは **breaking change** は導入されていませんが、OpenAI Responses ユーザー向けに重要な新機能領域として Responses API の websocket トランスポートサポートが含まれています。

ハイライト:

-   OpenAI Responses モデル向けに websocket トランスポートサポートを追加しました（オプトイン。既定のトランスポートは引き続き HTTP）。
-   マルチターン実行で websocket 対応プロバイダーと `RunConfig` を共有再利用するための `responses_websocket_session()` ヘルパー / `ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、フォローアップターンを網羅した新しい websocket ストリーミング例 (`examples/basic/stream_ws.py`) を追加しました。

### 0.9.0

このバージョンでは、 Python 3.9 は 3 か月前に EOL に到達したため、サポート対象外になりました。新しいランタイムバージョンにアップグレードしてください。

さらに、`Agent#as_tool()` メソッドの戻り値の型ヒントは `Tool` から `FunctionTool` に絞り込まれました。この変更は通常 breaking な問題を引き起こしませんが、より広い union 型にコードが依存している場合は、側でいくつか調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、2 つのランタイム動作変更により移行作業が必要になる場合があります。

- Function tools がラップする **同期** Python callable は、イベントループスレッド上で実行される代わりに `asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールロジックがスレッドローカル状態やスレッド依存リソースに依存している場合は、非同期ツール実装へ移行するか、ツールコード内でスレッド依存性を明示してください。
- ローカル MCP ツールの失敗処理が設定可能になり、既定の動作では実行全体を失敗させる代わりにモデルから可視なエラー出力を返せるようになりました。fail-fast セマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベル設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存アプリケーションに影響し得るいくつかの動作変更があります。

- ネストされたハンドオフ履歴は **オプトイン** になりました（デフォルトで無効）。v0.6.x の既定のネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1` / `gpt-5.2` の既定 `reasoning.effort` は `"none"` に変更されました（ SDK 既定値で設定されていた以前の既定 `"low"` から変更）。プロンプトや品質/コストプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、既定のハンドオフ履歴が raw の user/assistant ターンを露出するのではなく、単一の assistant メッセージにまとめられるようになり、下流エージェントに簡潔で予測可能な要約を提供します
- 既存の単一メッセージのハンドオフトランスクリプトは、既定で `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" で始まるようになり、下流エージェントが明確にラベル付けされた要約を得られます

### 0.5.0

このバージョンでは目に見える breaking changes は導入されていませんが、新機能と内部のいくつかの重要な更新が含まれています。

- [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) を処理するための `RealtimeRunner` サポートを追加しました
- Python 3.14 互換性のために `Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x はサポートされなくなりました。この SDK と合わせて openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API サポートは gpt-realtime モデルおよびその API インターフェース（ GA バージョン）に移行します。

### 0.2.0

このバージョンでは、以前 `Agent` を引数に取っていたいくつかの箇所が、代わりに `AgentBase` を引数に取るようになりました。たとえば MCP サーバーの `list_tools()` 呼び出しです。これは純粋に型定義上の変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正するだけです。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されています。`MCPServer` をサブクラス化しているクラスでは、これらのパラメーターを追加する必要があります。