---
search:
  exclude: true
---
# リリースプロセス／変更履歴

このプロジェクトでは、`0.Y.Z` 形式のセマンティックバージョニングを一部変更して使用しています。先頭の `0` は、SDK が現在も急速に進化していることを示します。各要素は次のように更新します。

## マイナー（`Y`）バージョン

ベータとしてマークされていない公開インターフェースに **破壊的変更** がある場合、マイナーバージョン `Y` を上げます。たとえば、`0.0.x` から `0.1.x` への移行には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することをお勧めします。

## パッチ（`Z`）バージョン

破壊的変更ではない次の変更については、`Z` を上げます。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## 破壊的変更の変更履歴

### 0.19.0

このマイナーリリースでは、破壊的変更は **ありません**。マイナーバージョンの更新は、OpenAI Responses の重要な新機能領域であるプログラマティックツール呼び出しを反映したものです。

注目点：

-   サポート対象の OpenAI Responses モデルが JavaScript を生成して、利用可能なツールを連携できるようにする [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] を追加しました。ツールごとの `allowed_callers`、構造化された関数ツール出力、および Runner のストリーミング、ガードレール、承認、セッション、`RunState` との統合をサポートします。セットアップと制約については、[プログラマティックツール呼び出し](tools.md#programmatic-tool-calling)を参照してください。
-   公開 `agents.decorators` モジュールと、既存の関数およびガードレール用デコレーターに加えて、より短い `@tool` エイリアスを追加しました。関数ツールは、非同期 callable オブジェクトもサポートするようになりました。
-   SDK の設定では、エージェント、実行、モデル、セッション、サンドボックス、音声パイプライン全体で、型付き設定オブジェクトまたは辞書のいずれかを一貫して受け入れるようになり、不明な設定に対する検証も追加されました。
-   モデル、ツール、MCP、Realtime、セッション、サンドボックス、トレーシング全体のエラーおよび診断ログを強化し、有用なデバッグコンテキストを維持しながら、機密性の高い raw ペイロードが公開されることを防止しました。
-   AnyLLM、LiteLLM、Chat Completions との互換性を改善し、モデルの再試行をまたいでセッション履歴を維持するようにしました。また、レスポンス開始前に発生する WebSocket 過負荷に対するプロバイダー再試行のガイダンスを追加し、再実行が許可されている場合に、オプトインの Runner 再試行ポリシーが機能できるようにしました。
-   `VercelCloudBucketMountStrategy` を通じて、[Vercel サンドボックス向けの作成時限定 S3 マウント](sandbox/clients.md#mounts-and-remote-storage)を追加しました。マウントされたセッションでは、バケットの内容がワークスペースの永続化から除外されます。また、動的なマウント変更やセッション再開は意図的にサポートされていません。

### 0.18.0

このマイナーリリースでは、破壊的変更は **ありません**。マイナーバージョンの更新は、Realtime エージェントのデフォルトモデルの更新のみを反映したものです。

注目点：

-   Realtime エージェントのデフォルトモデルが `gpt-realtime-2.1` になり、新しい Realtime セットアップでは、追加設定なしで最新の推奨モデルが使用されるようになりました。

### 0.17.0

このバージョンでは、サンドボックスのローカルソースを実体化する際、ソースパスが `Manifest.extra_path_grants` の対象でない限り、`LocalFile.src` と `LocalDir.src` が実体化先の `base_dir` 内に維持されます。`base_dir` は、Manifest が適用された時点での SDK プロセスの現在の作業ディレクトリです。相対的なローカルソースはそのディレクトリを基準に解決され、絶対パスのローカルソースは、あらかじめそのディレクトリ内または明示的に許可された範囲内に存在する必要があります。これによりローカルアーティファクトの境界に関する問題は解消されますが、信頼済みのホストファイルやディレクトリを、そのベースディレクトリの外部からサンドボックスワークスペースへ意図的にコピーするアプリケーションには影響する可能性があります。

移行するには、Manifest レベルで `SandboxPathGrant` を使用して信頼済みのホストルートを許可してください。サンドボックスがそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることを推奨します。

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

`extra_path_grants` は、信頼済みのアプリケーション設定として扱ってください。アプリケーションが対象のホストパスを事前に承認していない限り、モデル出力やその他の信頼できない Manifest 入力から許可設定を追加しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` から `gpt-5.4-mini` に変更されました。これは、モデルを明示的に設定していないエージェントおよび実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙的なデフォルトモデル設定には、`reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルト値が含まれるようになりました。

以前のデフォルトモデルの動作を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に設定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

注目点：

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` で `max_turns=None` を指定し、ターン数の上限を無効化できるようになりました。
-   ローカル、Docker、プロバイダー提供のサンドボックス実装全体で、サンドボックスワークスペースのハイドレーション時に、絶対パスのシンボリックリンク先を含め、アーカイブのルート外を指すシンボリックリンクを含む tar アーカイブが拒否されるようになりました。

### 0.15.0

このバージョンでは、モデルによる拒否が空のテキスト出力として扱われたり、structured outputs の場合に `MaxTurnsExceeded` になるまで実行ループが再試行されたりする代わりに、`ModelRefusalError` として明示的に通知されるようになりました。

これは、拒否のみのモデルレスポンスが `final_output == ""` で完了することを期待していたコードに影響します。例外を発生させずに拒否を処理するには、`model_refusal` 実行エラーハンドラーを指定してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs を使用するエージェントでは、ハンドラーがエージェントの出力スキーマに一致する値を返すことができ、SDK はほかの実行エラーハンドラーの最終出力と同様に検証します。

### 0.14.0

このマイナーリリースでは破壊的変更は **ありません** が、主要な新しいベータ機能領域であるサンドボックスエージェントに加え、ローカル、コンテナ化、ホスト環境全体で使用するために必要なランタイム、バックエンド、ドキュメントのサポートが追加されています。

注目点：

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しいベータ版サンドボックスランタイムインターフェースを追加しました。これにより、エージェントは、ファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開機能を備えた永続的な分離ワークスペース内で作業できます。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` によるローカルおよびコンテナ化された開発向けのサンドボックス実行バックエンドに加え、オプションの extras を通じて Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel のホスト型プロバイダー統合を追加しました。
-   サンドボックスメモリのサポートを追加し、段階的開示、複数ターンのグループ化、設定可能な分離境界、S3 を利用するワークフローを含む永続化メモリのコード例により、今後の実行で過去の実行から得られた知見を再利用できるようにしました。
-   ローカルおよび合成ワークスペースエントリ、S3／R2／GCS／Azure Blob Storage／S3 Files 向けのリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットを使用する再開フローなど、より広範なワークスペースおよび再開モデルを追加しました。
-   `examples/sandbox/` 配下に多数のサンドボックスのコード例とチュートリアルを追加しました。スキル、ハンドオフ、メモリを使用するコーディングタスク、プロバイダー固有のセットアップ、コードレビュー、データルーム QA、Web サイトのクローン作成などのエンドツーエンドワークフローを扱っています。
-   サンドボックス対応のセッション準備、機能のバインディング、状態のシリアル化、統合トレーシング、プロンプトキャッシュキーのデフォルト値、機密性の高い MCP 出力をより安全に秘匿する処理により、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースでは破壊的変更は **ありません** が、注目すべき Realtime のデフォルト設定の更新、新しい MCP 機能、ランタイムの安定性向上が含まれています。

注目点：

-   デフォルトの WebSocket Realtime モデルが `gpt-realtime-1.5` になり、新しい Realtime エージェントのセットアップでは、追加設定なしでより新しいモデルが使用されるようになりました。
-   `MCPServer` で `list_resources()`、`list_resource_templates()`、`read_resource()` が公開されるようになりました。また、`MCPServerStreamableHttp` で `session_id` が公開され、ストリーミング可能な HTTP セッションを再接続後やステートレスワーカー間で再開できるようになりました。
-   Chat Completions 統合では、`should_replay_reasoning_content` を通じて推論コンテンツのリプレイをオプトインできるようになり、LiteLLM／DeepSeek などのアダプターで、プロバイダー固有の推論およびツール呼び出しの継続性が向上しました。
-   `SQLAlchemySession` における最初の書き込みの同時実行、推論除去後に孤立したアシスタントメッセージ ID を含む圧縮リクエスト、`remove_all_tools()` の実行後に残る MCP／推論項目、関数ツールのバッチ実行機構における競合状態など、ランタイムおよびセッションの複数のエッジケースを修正しました。

### 0.12.0

このマイナーリリースでは、破壊的変更は **ありません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースでは、破壊的変更は **ありません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースでは破壊的変更は **ありません** が、OpenAI Responses のユーザー向けの重要な新機能領域として、Responses API の WebSocket トランスポート対応が含まれています。

注目点：

-   OpenAI Responses モデルに WebSocket トランスポート対応を追加しました（オプトイン方式であり、HTTP が引き続きデフォルトのトランスポートです）。
-   複数ターンの実行間で、共有の WebSocket 対応プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー／`ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、後続ターンを扱う新しい WebSocket ストリーミングのコード例（`examples/basic/stream_ws.py`）を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 のメジャーバージョンが 3 か月前に EOL に達したため、Python 3.9 はサポートされなくなりました。より新しいランタイムバージョンにアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` に限定されました。通常、この変更が破壊的な問題を引き起こすことはありませんが、コードがより広範なユニオン型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、ランタイムの動作に関する次の 2 つの変更により、移行作業が必要になる可能性があります。

- **同期** Python callable をラップする関数ツールは、イベントループスレッド上で実行される代わりに、`asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールのロジックがスレッドローカル状態やスレッドアフィンなリソースに依存している場合は、非同期ツール実装へ移行するか、ツールコード内でスレッドアフィニティを明示してください。
- ローカル MCP ツールの失敗処理が設定可能になり、デフォルトの動作では実行全体を失敗させる代わりに、モデルから参照可能なエラー出力を返す場合があります。フェイルファストのセマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` の値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある動作変更がいくつかあります。

- ネストされたハンドオフ履歴は **オプトイン** になりました（デフォルトでは無効）。v0.6.x でデフォルトだったネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1`／`gpt-5.2` のデフォルトの `reasoning.effort` が、SDK のデフォルト設定で構成されていた以前のデフォルト値 `"low"` から `"none"` に変更されました。プロンプトまたは品質／コスト特性が `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴が、raw なユーザー／アシスタントのターンを公開する代わりに、単一のアシスタントメッセージにまとめられるようになり、後続のエージェントに簡潔で予測可能な要約が提供されます
- 既存の単一メッセージによるハンドオフ記録は、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" から始まるようになり、後続のエージェントが明確にラベル付けされた要約を受け取れるようになりました

### 0.5.0

このバージョンでは、目に見える破壊的変更は導入されていませんが、新機能と内部実装に関するいくつかの重要な更新が含まれています。

- `RealtimeRunner` が [SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理できるようになりました
- Python 3.14 との互換性のために、`Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK と併用する場合は、openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API のサポートが gpt-realtime モデルとその API インターフェース（GA 版）へ移行します。

### 0.2.0

このバージョンでは、以前は `Agent` を引数として受け取っていたいくつかの箇所が、代わりに `AgentBase` を引数として受け取るようになりました。たとえば、MCP サーバーの `list_tools()` 呼び出しです。これは純粋に型付けのみの変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正するだけです。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されました。`MCPServer` を継承するすべてのクラスに、これらのパラメーターを追加する必要があります。