---
search:
  exclude: true
---
# リリースプロセス / 変更履歴

このプロジェクトでは、`0.Y.Z` 形式を使用する、semantic versioning をやや修正したバージョニングを採用しています。先頭の `0` は、この SDK がまだ急速に進化していることを示します。各コンポーネントは次のように増分されます。

## マイナー (`Y`) バージョン

ベータとしてマークされていない公開インターフェースに **破壊的変更** がある場合、マイナーバージョン `Y` を上げます。たとえば、`0.0.x` から `0.1.x` への移行には破壊的変更が含まれる可能性があります。

破壊的変更を望まない場合は、プロジェクト内で `0.0.x` バージョンに固定することを推奨します。

## パッチ (`Z`) バージョン

破壊的ではない変更については `Z` を増やします。

- バグ修正
- 新機能
- 非公開インターフェースの変更
- ベータ機能の更新

## 破壊的変更の変更履歴

### 0.14.0

このマイナーリリースでは **破壊的変更** は導入されませんが、新しい主要なベータ機能領域として Sandbox Agents が追加されています。また、ローカル環境、コンテナ化環境、ホスト環境でそれらを使用するために必要なランタイム、バックエンド、ドキュメントのサポートも含まれています。

主なポイント:

- `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とした新しいベータ sandbox runtime surface を追加し、ファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開サポートを備えた永続的で隔離されたワークスペース内でエージェントが動作できるようにしました。
- `UnixLocalSandboxClient` と `DockerSandboxClient` により、ローカルおよびコンテナ化された開発向けの sandbox 実行バックエンドを追加しました。さらに、オプションの extra を通じて Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel 向けのホスト型プロバイダー統合も追加しました。
- 将来の実行で過去の実行から得た学びを再利用できるように sandbox memory support を追加しました。これには progressive disclosure、複数ターンのグルーピング、設定可能な分離境界、S3 ベースのワークフローを含む永続化メモリーの例が含まれます。
- より広範なワークスペースおよび再開モデルを追加しました。これには、ローカルおよび合成ワークスペースエントリー、S3 / R2 / GCS / Azure Blob Storage / S3 Files 向けのリモートストレージマウント、ポータブルなスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットを介した再開フローが含まれます。
- `examples/sandbox/` 以下に充実した sandbox のコード例とチュートリアルを追加しました。skills、ハンドオフ、メモリー、プロバイダー固有のセットアップ、コードレビュー、dataroom QA、Web サイトのクローン作成などのエンドツーエンドワークフローを用いたコーディングタスクを扱っています。
- sandbox 対応のセッション準備、capability binding、状態のシリアライズ、統合トレーシング、prompt cache key のデフォルト、および機微な MCP 出力のより安全な秘匿化を含めて、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースでは **破壊的変更** は導入されませんが、注目すべき Realtime のデフォルト更新に加えて、新しい MCP 機能とランタイム安定性の修正が含まれています。

主なポイント:

- デフォルトの websocket Realtime モデルが `gpt-realtime-1.5` になり、新しい Realtime エージェント構成では追加設定なしで新しいモデルが使用されるようになりました。
- `MCPServer` は `list_resources()`、`list_resource_templates()`、`read_resource()` を公開するようになり、`MCPServerStreamableHttp` は `session_id` を公開するようになったため、streamable HTTP セッションを再接続時やステートレスなワーカー間で再開できるようになりました。
- Chat Completions 統合で `should_replay_reasoning_content` による reasoning-content の再生を選択できるようになり、LiteLLM / DeepSeek などのアダプターにおいて、プロバイダー固有の reasoning / tool-call の継続性が向上しました。
- `SQLAlchemySession` における同時の最初の書き込み、reasoning の除去後に assistant message ID が孤立した compaction リクエスト、`remove_all_tools()` で MCP / reasoning 項目が残る問題、関数ツールのバッチエグゼキューターにおける競合など、複数のランタイムおよびセッションのエッジケースを修正しました。

### 0.12.0

このマイナーリリースでは **破壊的変更** は導入されません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0) を確認してください。

### 0.11.0

このマイナーリリースでは **破壊的変更** は導入されません。主要な機能追加については [リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0) を確認してください。

### 0.10.0

このマイナーリリースでは **破壊的変更** は導入されませんが、OpenAI Responses ユーザー向けの重要な新機能領域として Responses API の websocket transport support が含まれています。

主なポイント:

- OpenAI Responses モデル向けに websocket transport support を追加しました（オプトイン方式で、既定の transport は引き続き HTTP です）。
- 複数ターンの実行にまたがって websocket 対応の共有プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー / `ResponsesWebSocketSession` を追加しました。
- ストリーミング、tools、承認、フォローアップターンを扱う新しい websocket ストリーミングのコード例 (`examples/basic/stream_ws.py`) を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 は 3 か月前に EOL に達したため、サポート対象外となりました。より新しいランタイムバージョンにアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントは、`Tool` から `FunctionTool` に絞り込まれました。この変更は通常、破壊的な問題を引き起こすことはありませんが、コードがより広い union type に依存している場合は、利用側でいくつか調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、ランタイム動作の 2 つの変更により、移行作業が必要になる場合があります。

- **同期的な** Python callable をラップする関数ツールは、イベントループスレッド上で実行されるのではなく、`asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールロジックがスレッドローカルな状態やスレッドに紐づくリソースに依存している場合は、非同期ツール実装へ移行するか、ツールコード内でスレッド親和性を明示してください。
- ローカル MCP ツールの失敗処理が設定可能になり、デフォルト動作では実行全体を失敗させる代わりに、モデルから見えるエラー出力を返す場合があります。fail-fast の意味論に依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` の値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性のある動作変更がいくつかあります。

- ネストされたハンドオフ履歴は現在 **オプトイン** です（デフォルトでは無効）。v0.6.x のデフォルトのネスト動作に依存していた場合は、明示的に `RunConfig(nest_handoff_history=True)` を設定してください。
- `gpt-5.1` / `gpt-5.2` に対するデフォルトの `reasoning.effort` は `"none"` に変更されました（SDK デフォルトで設定されていた従来の `"low"` から変更）。プロンプトや品質 / コストプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴は、生の user / assistant ターンを公開する代わりに、単一の assistant メッセージにまとめられるようになり、下流エージェントに簡潔で予測可能な要約を提供します。
- 既存の単一メッセージのハンドオフトランスクリプトは、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" で始まるようになり、下流エージェントが明確にラベル付けされた要約を受け取れるようになりました。

### 0.5.0

このバージョンでは、目に見える破壊的変更は導入されませんが、新機能と内部的な重要更新がいくつか含まれています。

- `RealtimeRunner` が [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) を扱えるようサポートを追加しました
- Python 3.14 互換性のために `Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x 系はサポート対象外となりました。この SDK と合わせて openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API のサポートが gpt-realtime モデルおよびその API インターフェース（ GA 版）に移行します。

### 0.2.0

このバージョンでは、これまで引数として `Agent` を受け取っていたいくつかの箇所が、代わりに `AgentBase` を受け取るようになりました。たとえば、MCP サーバー内の `list_tools()` 呼び出しです。これは純粋に型に関する変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正してください。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に 2 つの新しい params が追加されています: `run_context` と `agent` です。`MCPServer` をサブクラス化しているすべてのクラスに、これらの params を追加する必要があります。