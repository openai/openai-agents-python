---
search:
  exclude: true
---
# リリースプロセス / 変更履歴

このプロジェクトは、`0.Y.Z` という形式を用いた、セマンティックバージョニングを少し変更した方式に従います。先頭の `0` は、SDK がまだ急速に進化していることを示します。各コンポーネントは次のように増やします。

## マイナー（`Y`）バージョン

beta としてマークされていない公開インターフェイスに対する **破壊的変更** がある場合、マイナーバージョン `Y` を増やします。たとえば、`0.0.x` から `0.1.x` への移行には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することを推奨します。

## パッチ（`Z`）バージョン

非破壊的な変更では `Z` を増やします。

- バグ修正
- 新機能
- private インターフェイスへの変更
- beta 機能の更新

## 破壊的変更の変更履歴

### 0.17.0

このバージョンでは、sandbox のローカルソースの materialization において、ソースパスが `Manifest.extra_path_grants` で対象に含まれていない限り、`LocalFile.src` と `LocalDir.src` は materialization の `base_dir` 内に保持されます。`base_dir` は manifest が適用されるときの SDK プロセスの現在の作業ディレクトリです。相対ローカルソースはそのディレクトリから解決され、一方で絶対ローカルソースは、すでにその中にあるか、明示的な grant の配下にある必要があります。これによりローカル artifact の境界に関する問題は解消されますが、そのベースディレクトリの外にある信頼済みのホストファイルやディレクトリを sandbox ワークスペースへ意図的にコピーするアプリケーションに影響する可能性があります。

移行するには、manifest レベルで `SandboxPathGrant` を使って信頼済みのホスト root を許可してください。sandbox がそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることが望ましいです。

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

`extra_path_grants` は、信頼済みのアプリケーション設定として扱ってください。アプリケーションがそれらのホストパスをすでに承認していない限り、モデル出力やその他の信頼できない manifest 入力から grant を設定しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` ではなく `gpt-5.4-mini` になりました。これは、モデルを明示的に設定していないエージェントと run に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙のデフォルトモデル設定には `reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルトが含まれるようになりました。

以前のデフォルトモデルの挙動を維持する必要がある場合は、エージェントまたは run config にモデルを明示的に設定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

ハイライト:

- `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` は、ターン制限を無効化するために `max_turns=None` を受け付けるようになりました。
- sandbox ワークスペースの hydration は、ローカル、Docker、およびプロバイダーが支援する sandbox 実装全体で、絶対 symlink ターゲットを含め、アーカイブ root の外を指す symlink を含む tar アーカイブを拒否するようになりました。

### 0.15.0

このバージョンでは、モデルの拒否が、空のテキスト出力として扱われたり、structured outputs の場合に `MaxTurnsExceeded` になるまで run ループでリトライされるのではなく、`ModelRefusalError` として明示的に表面化されるようになりました。

これは、以前は拒否のみのモデル応答が `final_output == ""` で完了することを期待していたコードに影響します。例外を発生させずに拒否を処理するには、`model_refusal` run error handler を提供してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured-output エージェントでは、handler はエージェントの出力スキーマに一致する値を返すことができ、SDK は他の run error handler の最終出力と同様に検証します。

### 0.14.0

このマイナーリリースでは破壊的変更は導入 **されません** が、大きな新しい beta 機能領域である Sandbox Agents と、それらをローカル、コンテナ化、ホスト環境全体で使用するために必要なランタイム、バックエンド、ドキュメントサポートが追加されています。

ハイライト:

- `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しい beta sandbox ランタイムサーフェスを追加し、エージェントがファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、resume サポートを備えた永続的に分離されたワークスペース内で作業できるようにしました。
- `UnixLocalSandboxClient` と `DockerSandboxClient` によるローカルおよびコンテナ化開発向けの sandbox 実行バックエンドに加え、optional extras を通じて Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel 向けのホスト型プロバイダー連携を追加しました。
- 将来の run が過去の run から得た教訓を再利用できるようにする sandbox メモリサポートを追加しました。progressive disclosure、multi-turn grouping、設定可能な分離境界、S3 backed ワークフローを含む永続化メモリのコード例が含まれます。
- ローカルおよび合成ワークスペースエントリ、S3/R2/GCS/Azure Blob Storage/S3 Files 向けのリモートストレージマウント、ポータブルスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットによる resume フローを含む、より広範なワークスペースおよび resume モデルを追加しました。
- `examples/sandbox/` 配下に、skills、ハンドオフ、メモリ、プロバイダー固有のセットアップを用いたコーディングタスク、およびコードレビュー、dataroom QA、Web サイトクローンなどのエンドツーエンドワークフローを扱う、実質的な sandbox のコード例とチュートリアルを追加しました。
- sandbox 対応の session 準備、capability binding、状態シリアライズ、統合トレーシング、prompt cache key のデフォルト、より安全な機密 MCP 出力の redaction により、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースでは破壊的変更は導入 **されません** が、注目すべき Realtime のデフォルト更新に加え、新しい MCP 機能とランタイム安定性の修正が含まれています。

ハイライト:

- デフォルトの websocket Realtime モデルは `gpt-realtime-1.5` になりました。これにより、新しい Realtime エージェントのセットアップでは追加設定なしでより新しいモデルが使用されます。
- `MCPServer` は `list_resources()`、`list_resource_templates()`、`read_resource()` を公開するようになり、`MCPServerStreamableHttp` は `session_id` を公開するようになりました。これにより、streamable HTTP セッションを再接続やステートレス worker 間で再開できます。
- Chat Completions 連携では、`should_replay_reasoning_content` によって reasoning-content replay を opt in できるようになり、LiteLLM/DeepSeek などの adapter におけるプロバイダー固有の reasoning/tool-call の継続性が改善されます。
- `SQLAlchemySession` における同時初回書き込み、reasoning stripping 後に孤立した assistant message ID を持つ compaction request、`remove_all_tools()` が MCP/reasoning item を残す問題、関数ツール batch executor の race など、いくつかのランタイムと session の edge case を修正しました。

### 0.12.0

このマイナーリリースでは破壊的変更は導入 **されません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースでは破壊的変更は導入 **されません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースでは破壊的変更は導入 **されません** が、OpenAI Responses ユーザー向けの重要な新機能領域である、Responses API の websocket transport サポートが含まれています。

ハイライト:

- OpenAI Responses モデル向けの websocket transport サポートを追加しました（opt-in。HTTP は引き続きデフォルトの transport です）。
- 共有の websocket 対応プロバイダーと `RunConfig` を複数ターンの run で再利用するための `responses_websocket_session()` helper / `ResponsesWebSocketSession` を追加しました。
- ストリーミング、ツール、承認、フォローアップターンを扱う新しい websocket ストリーミングコード例（`examples/basic/stream_ws.py`）を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 はサポートされなくなりました。このメジャーバージョンは 3 か月前に EOL に達したためです。より新しいランタイムバージョンへアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` に狭められました。この変更は通常、破壊的な問題を引き起こすことはありませんが、コードがより広い union 型に依存している場合は、側でいくつか調整が必要になる場合があります。

### 0.8.0

このバージョンでは、2 つのランタイム挙動の変更により、移行作業が必要になる場合があります。

- **同期** Python callable をラップする関数ツールは、イベントループスレッド上で実行されるのではなく、`asyncio.to_thread(...)` を介して worker thread 上で実行されるようになりました。ツールロジックが thread-local state や thread-affine resource に依存している場合は、async tool 実装へ移行するか、ツールコード内で thread affinity を明示してください。
- ローカル MCP ツールの失敗処理は設定可能になり、デフォルトの挙動では run 全体を失敗させる代わりに、モデルから見えるエラー出力を返す場合があります。fail-fast semantics に依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベルの設定を上書きするため、明示的な handler を持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある挙動変更がいくつかありました。

- ネストされたハンドオフ履歴は **opt-in**（デフォルトでは無効）になりました。v0.6.x のデフォルトのネスト挙動に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1` / `gpt-5.2` のデフォルトの `reasoning.effort` が `"none"` に変更されました（以前は SDK のデフォルトで設定された `"low"` がデフォルトでした）。プロンプトや品質 / コストプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴は raw のユーザー / assistant ターンを公開するのではなく、単一の assistant メッセージにまとめられるようになり、下流のエージェントに簡潔で予測可能な要約を提供します
- 既存の単一メッセージのハンドオフ transcript は、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" で始まるようになり、下流のエージェントに明確にラベル付けされた要約を提供します

### 0.5.0

このバージョンでは目に見える破壊的変更は導入されませんが、新機能と内部のいくつかの重要な更新が含まれています。

- [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) を処理するための `RealtimeRunner` のサポートを追加しました
- Python 3.14 互換性のために `Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK とともに openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API サポートが gpt-realtime モデルとその API インターフェイス（GA バージョン）へ移行します。

### 0.2.0

このバージョンでは、以前は `Agent` を arg として受け取っていたいくつかの箇所が、代わりに `AgentBase` を arg として受け取るようになりました。たとえば、MCP サーバーの `list_tools()` 呼び出しです。これは純粋に型付け上の変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正するだけです。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に 2 つの新しい params、`run_context` と `agent` が追加されました。`MCPServer` をサブクラス化するすべてのクラスに、これらの params を追加する必要があります。