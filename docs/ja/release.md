---
search:
  exclude: true
---
# リリースプロセス / 変更履歴

このプロジェクトは、形式 `0.Y.Z` を使用するセマンティックバージョニングを少し修正したものに従います。先頭の `0` は、SDK がまだ急速に進化中であることを示します。各構成要素は次のように増加させます:

## マイナー (`Y`) バージョン

ベータとしてマークされていない公開インターフェイスに対する **破壊的変更** の場合、マイナーバージョン `Y` を増やします。たとえば、`0.0.x` から `0.1.x` への移行には破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することをおすすめします。

## パッチ (`Z`) バージョン

非破壊的変更の場合は `Z` を増やします:

-   バグ修正
-   新機能
-   非公開インターフェイスの変更
-   ベータ機能の更新

## 破壊的変更の変更履歴

### 0.17.0

このバージョンでは、サンドボックスのローカルソースの実体化において、ソースパスが `Manifest.extra_path_grants` によってカバーされていない限り、`LocalFile.src` と `LocalDir.src` は実体化時の `base_dir` 内に収められます。`base_dir` は、マニフェストが適用される時点での SDK プロセスの現在の作業ディレクトリです。相対ローカルソースはそのディレクトリから解決され、絶対ローカルソースはすでにそのディレクトリ内にあるか、明示的な許可の配下にある必要があります。これによりローカルアーティファクトの境界に関する問題は解消されますが、そのベースディレクトリ外から信頼済みホストのファイルまたはディレクトリをサンドボックスワークスペースへ意図的にコピーするアプリケーションに影響する可能性があります。

移行するには、信頼済みホストルートをマニフェストレベルで `SandboxPathGrant` により許可してください。サンドボックスがそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることをおすすめします:

```python
from pathlib import Path

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import Dir, LocalDir

# This is an absolute host path outside the SDK process base_dir.
TRUSTED_DOCS_ROOT = Path("/opt/my-app/docs")

manifest = Manifest(
    extra_path_grants=(
        # This host root is outside the SDK process base_dir, so the manifest must grant it.
        SandboxPathGrant(path=str(TRUSTED_DOCS_ROOT), read_only=True),
    ),
    entries={
        # No grant is needed for local sources that stay under the SDK process base_dir.
        "fixtures": LocalDir(src=Path("fixtures"), description="Local test fixtures."),
        # This entry reads from the granted host root and copies it into the sandbox workspace.
        "docs": LocalDir(src=TRUSTED_DOCS_ROOT, description="Trusted local documents."),
        # Dir creates a sandbox workspace directory; it does not read from the host filesystem.
        "output": Dir(description="Generated artifacts."),
    },
)
```

`extra_path_grants` は信頼済みアプリケーション設定として扱ってください。アプリケーションがそれらのホストパスをすでに承認していない限り、モデル出力やその他の信頼できないマニフェスト入力から許可を設定しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` ではなく `gpt-5.4-mini` になりました。これは、モデルを明示的に設定していないエージェントと実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙的なデフォルトモデル設定には `reasoning.effort="none"` や `verbosity="low"` などの GPT-5 デフォルトが含まれるようになりました。

以前のデフォルトモデルの挙動を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に設定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください:

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

主な変更点:

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` は、ターン制限を無効にするために `max_turns=None` を受け取れるようになりました。
-   サンドボックスワークスペースのハイドレーションは、ローカル、Docker、およびプロバイダーがバックするサンドボックス実装全体で、絶対シンボリックリンクターゲットを含む、アーカイブルートの外部を指すシンボリックリンクを含む tar アーカイブを拒否するようになりました。

### 0.15.0

このバージョンでは、モデルの拒否応答は、空のテキスト出力として扱われたり、structured outputs の場合に実行ループが `MaxTurnsExceeded` まで再試行したりするのではなく、`ModelRefusalError` として明示的に表面化されるようになりました。

これは、以前に拒否のみのモデル応答が `final_output == ""` で完了することを期待していたコードに影響します。例外を発生させずに拒否を処理するには、`model_refusal` 実行エラーハンドラーを提供してください:

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured-output エージェントの場合、ハンドラーはエージェントの出力スキーマに一致する値を返すことができ、SDK は他の実行エラーハンドラーの最終出力と同様に検証します。

### 0.14.0

このマイナーリリースでは、破壊的変更は **導入しません** が、主要な新しいベータ機能領域である Sandbox エージェントに加え、ローカル、コンテナ化、ホスト環境全体でそれらを使用するために必要なランタイム、バックエンド、ドキュメントのサポートが追加されています。

主な変更点:

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とした新しいベータのサンドボックスランタイムサーフェスを追加しました。これにより、エージェントはファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開サポートを備えた永続的な隔離ワークスペース内で動作できます。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` によるローカルおよびコンテナ化開発向けのサンドボックス実行バックエンドに加え、任意の extras を通じた Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel 向けのホスト型プロバイダー連携を追加しました。
-   将来の実行が以前の実行から得た知見を再利用できるように、サンドボックスメモリサポートを追加しました。段階的開示、複数ターンのグループ化、設定可能な隔離境界、および S3 バックのワークフローを含む永続化メモリのコード例が含まれます。
-   ローカルおよび合成ワークスペースエントリー、S3/R2/GCS/Azure Blob Storage/S3 Files 向けのリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットによる再開フローを含む、より広範なワークスペースと再開モデルを追加しました。
-   `examples/sandbox/` 配下に、充実したサンドボックスのコード例とチュートリアルを追加しました。スキル、ハンドオフ、メモリを用いたコーディングタスク、プロバイダー固有のセットアップ、コードレビュー、データルーム QA、Web サイトのクローン作成などのエンドツーエンドのワークフローを扱います。
-   サンドボックス対応のセッション準備、ケイパビリティバインディング、状態のシリアライズ、統合トレーシング、プロンプトキャッシュキーのデフォルト、より安全な機密 MCP 出力のマスキングにより、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースでは、破壊的変更は **導入しません** が、注目すべき Realtime のデフォルト更新に加え、新しい MCP 機能とランタイム安定性の修正が含まれます。

主な変更点:

-   デフォルトの WebSocket Realtime モデルは `gpt-realtime-1.5` になりました。そのため、新しい Realtime エージェントのセットアップでは、追加設定なしで新しいモデルが使用されます。
-   `MCPServer` は `list_resources()`、`list_resource_templates()`、`read_resource()` を公開するようになりました。また、`MCPServerStreamableHttp` は `session_id` を公開するようになったため、ストリーム可能な HTTP セッションを再接続やステートレスワーカーをまたいで再開できます。
-   Chat Completions 連携は、`should_replay_reasoning_content` によって推論コンテンツの再生をオプトインできるようになりました。これにより、LiteLLM/DeepSeek などのアダプターで、プロバイダー固有の推論 / ツール呼び出しの連続性が向上します。
-   `SQLAlchemySession` における初回書き込みの同時実行、推論の除去後に孤立した assistant メッセージ ID を持つ圧縮リクエスト、`remove_all_tools()` が MCP/reasoning 項目を残す問題、関数ツールバッチ実行器の競合など、複数のランタイムおよびセッションのエッジケースを修正しました。

### 0.12.0

このマイナーリリースでは、破壊的変更は **導入しません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースでは、破壊的変更は **導入しません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースでは、破壊的変更は **導入しません** が、OpenAI Responses ユーザー向けの重要な新機能領域である Responses API の WebSocket トランスポートサポートが含まれます。

主な変更点:

-   OpenAI Responses モデル向けの WebSocket トランスポートサポートを追加しました（オプトインです。HTTP は引き続きデフォルトのトランスポートです）。
-   複数ターンの実行全体で共有の WebSocket 対応プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー / `ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、フォローアップターンを扱う新しい WebSocket ストリーミングコード例（`examples/basic/stream_ws.py`）を追加しました。

### 0.9.0

このバージョンでは、このメジャーバージョンが 3 か月前に EOL に達したため、Python 3.9 はサポートされなくなりました。より新しいランタイムバージョンへアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` へ狭められました。この変更は通常、破壊的な問題を引き起こすことはありませんが、コードがより広い Union 型に依存している場合は、側でいくつか調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、2 つのランタイム挙動の変更により、移行作業が必要になる可能性があります:

-   **同期** Python 呼び出し可能オブジェクトをラップする関数ツールは、イベントループスレッドで実行されるのではなく、`asyncio.to_thread(...)` を介してワーカースレッドで実行されるようになりました。ツールのロジックがスレッドローカル状態やスレッドアフィンなリソースに依存している場合は、async ツール実装へ移行するか、ツールコード内でスレッドアフィニティを明示してください。
-   ローカル MCP ツールの失敗処理は設定可能になり、デフォルトの挙動では実行全体を失敗させる代わりに、モデルから見えるエラー出力を返す場合があります。フェイルファストのセマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある挙動の変更がいくつかありました:

-   ネストされたハンドオフ履歴は **オプトイン** になりました（デフォルトでは無効）。v0.6.x のデフォルトのネスト挙動に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
-   `gpt-5.1` / `gpt-5.2` のデフォルトの `reasoning.effort` は、`"none"` に変更されました（SDK デフォルトで設定されていた以前のデフォルト `"low"` からの変更です）。プロンプトや品質 / コストのプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴は、生のユーザー / アシスタントターンを公開するのではなく、単一の assistant メッセージにまとめられるようになり、後続のエージェントに簡潔で予測可能な要約を提供します
-   既存の単一メッセージのハンドオフトランスクリプトは、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" で始まるようになったため、後続のエージェントは明確なラベル付きの要約を受け取れます

### 0.5.0

このバージョンでは、目に見える破壊的変更は導入されませんが、新機能と内部のいくつかの重要な更新が含まれます:

-   `RealtimeRunner` が [SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理するためのサポートを追加しました
-   Python 3.14 互換性のために、`Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK とともに openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API サポートは gpt-realtime モデルとその API インターフェイス（GA バージョン）へ移行します。

### 0.2.0

このバージョンでは、以前は `Agent` を引数として受け取っていたいくつかの箇所が、代わりに `AgentBase` を引数として受け取るようになりました。たとえば、MCP サーバーの `list_tools()` 呼び出しです。これは純粋に型付け上の変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正するだけです。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されました。`MCPServer` をサブクラス化しているすべてのクラスに、これらのパラメーターを追加する必要があります。