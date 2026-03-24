---
search:
  exclude: true
---
# リリースプロセス / 変更履歴

このプロジェクトは、`0.Y.Z` 形式を使用する、semantic versioning のやや変更された版に従います。先頭の `0` は、この SDK が依然として急速に進化していることを示します。各コンポーネントのインクリメントは以下のとおりです。

## マイナー (`Y`) バージョン

beta としてマークされていない公開インターフェースに対する **破壊的変更** がある場合、マイナーバージョン `Y` を上げます。たとえば、`0.0.x` から `0.1.x` への変更には破壊的変更が含まれる可能性があります。

破壊的変更を望まない場合は、プロジェクトで `0.0.x` バージョンに固定することを推奨します。

## パッチ (`Z`) バージョン

非破壊的変更については `Z` を上げます。

-   バグ修正
-   新機能
-   プライベートインターフェースへの変更
-   beta 機能の更新

## 破壊的変更の変更履歴

### 0.13.0

このマイナーリリースでは **破壊的変更** は導入されませんが、注目すべき Realtime のデフォルト更新、新しい MCP 機能、ランタイム安定性の修正が含まれます。

ハイライト:

-   デフォルトの websocket Realtime モデルは `gpt-realtime-1.5` になり、新しい Realtime エージェントのセットアップでは追加設定なしで新しいモデルを使用します。
-   `MCPServer` は `list_resources()`、`list_resource_templates()`、`read_resource()` を公開し、`MCPServerStreamableHttp` は `session_id` を公開するようになったため、streamable HTTP セッションを再接続時やステートレスワーカー間で再開できます。
-   Chat Completions 統合は `should_replay_reasoning_content` による reasoning-content の再生を選択できるようになり、LiteLLM / DeepSeek などのアダプターでプロバイダー固有の reasoning / tool-call 継続性が向上しました。
-   `SQLAlchemySession` の同時初回書き込み、reasoning 削除後に assistant message ID が孤立した compaction リクエスト、`remove_all_tools()` が MCP / reasoning 項目を残してしまう問題、関数ツールのバッチエグゼキューターでの競合など、複数のランタイムおよびセッションの境界ケースを修正しました。

### 0.12.0

このマイナーリリースでは **破壊的変更** は導入されません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0) を確認してください。

### 0.11.0

このマイナーリリースでは **破壊的変更** は導入されません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0) を確認してください。

### 0.10.0

このマイナーリリースでは **破壊的変更** は導入されませんが、OpenAI Responses ユーザー向けに重要な新機能領域として、Responses API の websocket transport サポートが含まれます。

ハイライト:

-   OpenAI Responses モデル向けに websocket transport サポートを追加しました（オプトイン。デフォルト transport は引き続き HTTP）。
-   複数ターンの実行で共有の websocket 対応プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー / `ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、フォローアップターンを扱う新しい websocket ストリーミング例（`examples/basic/stream_ws.py`）を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 はサポート対象外になりました。このメジャーバージョンは 3 か月前に EOL に到達しています。新しいランタイムバージョンへアップグレードしてください。

さらに、`Agent#as_tool()` メソッドが返す値の型ヒントは、`Tool` から `FunctionTool` へ狭められました。この変更は通常は破壊的な問題を引き起こしませんが、コードがより広い union 型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、2 つのランタイム動作変更により移行作業が必要になる可能性があります。

- Function tools でラップされた **同期** Python callable は、イベントループスレッド上で実行される代わりに `asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールロジックがスレッドローカル状態やスレッド親和性のあるリソースに依存する場合は、非同期ツール実装へ移行するか、ツールコード内でスレッド親和性を明示してください。
- ローカル MCP ツールの失敗処理は設定可能になり、デフォルト動作では実行全体を失敗させる代わりにモデル可視のエラー出力を返せるようになりました。fail-fast セマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベル設定より優先されるため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存アプリケーションに影響しうるいくつかの動作変更があります。

- ネストしたハンドオフ履歴は **オプトイン** になりました（デフォルトで無効）。v0.6.x のデフォルトのネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1` / `gpt-5.2` のデフォルト `reasoning.effort` は `"none"` に変更されました（SDK デフォルトで設定されていた以前のデフォルト `"low"` から変更）。プロンプトや品質 / コストプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴は生のユーザー / assistant ターンを公開する代わりに、単一の assistant メッセージにまとめられるようになり、下流エージェントに簡潔で予測可能な要約を提供します
- 既存の単一メッセージのハンドオフトランスクリプトは、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" で始まるようになり、下流エージェントが明確にラベル付けされた要約を得られます

### 0.5.0

このバージョンでは、目に見える破壊的変更はありませんが、新機能と内部の重要な更新がいくつか含まれます。

- `RealtimeRunner` が [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) を処理するサポートを追加しました
- Python 3.14 互換性のために `Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x はサポート対象外になりました。この SDK と合わせて openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API サポートが gpt-realtime モデルとその API インターフェース（ GA バージョン）に移行します。

### 0.2.0

このバージョンでは、これまで引数として `Agent` を受け取っていたいくつかの箇所が、代わりに `AgentBase` を受け取るようになりました。たとえば、MCP サーバー内の `list_tools()` 呼び出しです。これは純粋に型付け上の変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正してください。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に 2 つの新しいパラメーター `run_context` と `agent` が追加されました。`MCPServer` を継承するクラスには、これらのパラメーターを追加する必要があります。