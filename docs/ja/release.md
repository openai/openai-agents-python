---
search:
  exclude: true
---
# リリースプロセス／変更履歴

このプロジェクトでは、`0.Y.Z` 形式を使用した、セマンティックバージョニングを一部変更した方式に従います。先頭の `0` は、SDK がまだ急速に進化していることを示します。各要素は次のように更新します。

## マイナー（`Y`）バージョン

ベータと明記されていない公開インターフェースに **破壊的変更** がある場合、マイナーバージョン `Y` を上げます。たとえば、`0.0.x` から `0.1.x` への更新には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することをお勧めします。

## パッチ（`Z`）バージョン

破壊的でない変更については、`Z` を上げます。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## 破壊的変更の変更履歴

### 0.21.0

バージョン 0.21.0 では `openai` v3 が必要となり、Agents SDK の OpenAI HTTP 統合が HTTPX2 に移行します。デフォルトの OpenAI クライアントを使用するアプリケーションではクライアント設定を変更する必要はありませんが、OpenAI HTTP レイヤーをカスタマイズしているアプリケーションでは、トランスポート関連コードの移行が必要になる場合があります。

主な変更点：

-   必須の OpenAI 依存関係は `openai>=3.0.0,<4` になりました。クリーンなコアインストールでは HTTPX2 が使用され、従来の `httpx` は直接の依存関係としてインストールされなくなりました。
-   デフォルトの OpenAI プロバイダー、音声プロバイダー、Responses WebSocket 対応、トレーシングエクスポーター、プロバイダー再試行の正規化で、HTTPX2 が使用されるようになりました。既存の Agents SDK の公開設定と実行時動作に変更はありません。
-   `AsyncOpenAI` に `http_client=` を渡すアプリケーションでは、カスタムクライアント、トランスポート、認証、イベントフック、モックトランスポート、タイムアウト値、URL、リクエスト、レスポンス、トランスポート例外処理を `httpx` から `httpx2` に移行する必要があります。OpenAI クライアントのデフォルト設定に加えてカスタム HTTP オプションが必要な場合は、OpenAI Python SDK の `DefaultAsyncHttpx2Client` を推奨します。[`openai` v3 でのカスタム HTTP クライアント](config.md#custom-http-clients-with-openai-v3)を参照してください。
-   Agents SDK は、任意の従来型 HTTPX オブジェクトを HTTPX2 に変換しません。OpenAI Python SDK の一時的な従来型クライアント互換パスには、`httpx` の明示的なインストールが必要であり、移行用の橋渡しとして扱う必要があります。
-   ローカル MCP の HTTP カスタマイズは、引き続きインストール済みの MCP パッケージに従います。MCP Python SDK v1 は従来の `httpx` を提供して使用し、MCP Python SDK v2 は `httpx2` を使用します。通常の MCP 接続では、アプリケーションを変更する必要はありません。[MCP Python SDK v1 および v2](mcp.md#mcp-python-sdk-v1-and-v2)を参照してください。
-   公開されたプロバイダー非依存のテストユーティリティで、プロバイダーやプロセスへの依存なしに、エージェントモデル、サンドボックスセッション、Realtime セッション、音声パイプラインのワークフローを扱えるようになりました。レシピ、および実際のプロバイダーアダプターや統合境界を維持すべき場合のガイダンスについては、[テスト](testing.md)を参照してください。

### 0.20.0

バージョン 0.20.0 には、ローカル MCP HTTP トランスポートをカスタマイズするアプリケーションにとって、破壊的変更となる可能性がある MCP 依存関係の移行が含まれます。また、エージェントまたは実行でモデルを明示的に選択しない場合に使用される SDK のデフォルトモデルも更新されます。

主な変更点：

-   SDK のデフォルトモデルは、`gpt-5.4-mini` ではなく `gpt-5.6-luna` になりました。デフォルトの `reasoning.effort="none"` および `verbosity="low"` 設定に変更はありません。
-   エージェントで明示的に指定したモデル、実行レベルのモデルオーバーライド、および `OPENAI_DEFAULT_MODEL` 環境変数は、引き続き SDK のデフォルトより優先されます。
-   Realtime 入力文字起こし設定で、`gpt-transcribe`、`gpt-live-transcribe`、`gpt-realtime-whisper` が認識されるようになりました。低レイテンシーの `gpt-live-transcribe` セッションでは、ネストされた `audio.input.transcription` 設定で `prompt`、`keywords`、および複数の想定される `languages` を指定できます。この SDK が固定している OpenAI クライアントバージョンは、`delay` のレイテンシー／精度レベルを `gpt-realtime-whisper` でのみサポートします。確定済みの音声ターン後の文字起こし、または検出言語の出力には、WebSocket 経由で `gpt-transcribe` を使用してください。`audio.input.turn_detection=None` を明示的に設定すると、自動ターン検出が無効になります。[入力文字起こし設定](realtime/guide.md#input-transcription-settings)を参照してください。
-   Agents SDK によって作成されるローカル MCP 接続は、`mcp>=1.19.0,<3` を通じて v1 互換性を維持しながら、MCP Python SDK v2 をサポートするようになりました。Agents SDK は、通常の stdio、SSE、Streamable HTTP 接続を自動的に適応させます。MCP v2 がインストールされている場合、これらの接続は `mcp.Client(mode="auto")` を使用してサポート対象の最新プロトコルを検出し、古いサーバーでは従来の `initialize` ハンドシェイクにフォールバックします。依存関係の解決で MCP v2 が選択された場合、カスタム `httpx.Auth` オブジェクトまたは `httpx.AsyncClient` ファクトリーを提供するアプリケーションは、それらの値を `httpx2` に移行する必要があります。あるいは、v1 の HTTP スタックを維持するには `mcp<2` に固定してください。`MCPServerStreamableHttp` の `params["ignore_initialized_notification_failure"] = True` オプションも、引き続き v1 専用です。移行の詳細については、[MCP Python SDK v1 および v2](mcp.md#mcp-python-sdk-v1-and-v2)を参照してください。
-   サンドボックスのマウント検証では、サンドボックスまたはマウントヘルパーで副作用が発生する前に、安全でない認証情報の配置を拒否するようになりました。信頼できるアプリケーションでは、ストレージ機能テーブルを変更することなく、コンテナー内の正確なマウントパスについて、マウント範囲または広範囲の認証情報公開を承認できます。これらの承認は実行時にのみ有効であり、シリアライズされたサンドボックス状態だけで認証情報への権限が付与されることはありません。保護されたマウント境界では、SDK は新たにリダクトされた例外を返します。元の例外が、SDK で正確に認識されるサンドボックスエラーであり、承認された構造化フィールドが検証に合格した場合、置換後の例外にはそのサブタイプと検証済みの安全なフィールドが保持されます。認識された `MountConfigError` では、SDK が生成した安全な検証メッセージも保持できます。それ以外の場合、SDK は新たに汎用のリダクト済みエラーを返します。プロバイダーが制御するメッセージ、その他の未承認メッセージ、コマンドデータ、注記、コンテキスト、原因、および元のトレースバック状態は保持されません。[マウントとリモートストレージ](sandbox/clients.md#mounts-and-remote-storage)および[セッション状態からの再開](sandbox/guide.md#resume-from-session-state)を参照してください。
-   再試行ポリシーでは、安定したリプレイ安全性情報を確認し、プロバイダーが安全でないと判断した非ストリーミングリクエストに対して `RetryDecision(approve_unsafe_replay=True)` を明示的に設定できます。この承認によって、中止、送出済みのストリーミング出力、または Programmatic Tool Calling などのローカル側の副作用に対する個別の拒否を回避することはできません。[Runner が管理する再試行](models/index.md#runner-managed-retries)を参照してください。
-   再開可能な `RunState` オブジェクトでは、次回のモデル呼び出し前に `add_input()` を使用して永続的なユーザー入力をステージングできるようになりました。ステージングされた入力はシリアライズ後も保持され、入力ガードレールを通過し、ローカルセッションとサーバー管理の会話全体で永続的な SDK 入力を 1 回生成します。安全でないリプレイを明示的に承認した場合は、引き続き入力がプロバイダーに再送信され、プロバイダー側の処理が繰り返される可能性があります。[再開前の入力追加](results.md#add-input-before-resuming)を参照してください。
-   実行時の信頼性に関する修正により、ストリーミングと非ストリーミングの[出力ガードレールにおけるセッション永続化](guardrails.md#output-guardrails)の動作が統一され、コピーおよび名前空間設定の際に `FunctionTool` のサブクラスが保持されるようになりました。また、空のストリームを暗黙的に完了する代わりに、[サポートされていない Chat Completions 音声出力](models/index.md#chat-completions-compatibility-options)に対して明示的なエラーが発生するようになりました。`OpenAIResponsesCompactionSession` ラッパーは、キャンセルが呼び出し元に到達する前に、[コンパクション前の履歴復元](sessions/index.md#auto-compaction-can-block-streaming)を試行して完了を待ちます。[`VoicePipeline`](voice/pipeline.md#results) のコンシューマーは、正常な実行後に文字起こしセッションのクローズが失敗した場合、その失敗を受け取るようになりました。一方、先にターンが失敗していた場合は、後から発生したクローズ失敗よりも優先されます。`RunState` のラウンドトリップでは、ローカルシェル出力、承認済みのコンピューター安全性チェック、デフォルト値を持つツール出力フィールド、および辞書、リスト、タプルの走査中に検出された Pydantic モデルまたはデータクラスの出力が保持されるようになりました。MCP 変換では、自由形式のオブジェクトスキーマと画像出力が保持され、音声ブロックやリソースブロックなど、その他の raw コンテンツブロックは有効な JSON テキストとしてシリアライズされます。`MCPServerManager` は、重複するライフサイクル操作を直列化し、接続とクリーンアップに有限のデフォルトタイムアウトを適用します。モデルのリプレイでは、出力項目を入力として使用する前に、サーバーが所有する `created_by` メタデータが削除されます。

### 0.19.0

このマイナーリリースに破壊的変更は **ありません**。マイナーバージョンの更新は、OpenAI Responses の重要な新機能領域である Programmatic Tool Calling を反映したものです。

主な変更点：

-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] が追加されました。これにより、対応する OpenAI Responses モデルは JavaScript を生成し、Programmatic Tool Calling の対象となるツールを連携させることができます。ツールごとの `allowed_callers`、`FunctionTool` インスタンスからの structured outputs、および Runner のストリーミング、ガードレール、承認、セッション、`RunState` との統合をサポートします。設定と制約については、[Programmatic Tool Calling](tools.md#programmatic-tool-calling)を参照してください。
-   公開 `agents.decorators` モジュールと、既存の `@function_tool` デコレーターの短いエイリアスである `@tool` が、既存のガードレールデコレーターとともに追加されました。`FunctionTool` インスタンスでは、非同期の呼び出し可能オブジェクトもサポートされるようになりました。
-   SDK 設定では、エージェント、実行、モデル、セッション、サンドボックス、音声パイプラインの全体で、型付き設定オブジェクトまたは辞書のいずれかを一貫して受け付けるようになり、未知の設定に対する検証も追加されました。
-   モデル、ツール、MCP、Realtime、セッション、サンドボックス、トレーシング全体で、エラーおよび診断ログが強化され、有用なデバッグコンテキストを維持しながら、raw の機密ペイロードが公開されないようになりました。
-   AnyLLM、LiteLLM、Chat Completions との互換性が向上し、モデルの再試行をまたいでセッション履歴が保持されるようになりました。また、レスポンス開始前に発生する WebSocket 過負荷に対するプロバイダー再試行のガイダンスが追加され、許可されている場合は、オプトインした Runner 再試行ポリシーで失敗した試行をリプレイできるようになりました。
-   `VercelCloudBucketMountStrategy` を通じて、[Vercel サンドボックスの作成時にのみ設定できる S3 マウント](sandbox/clients.md#mounts-and-remote-storage)が追加されました。マウントされたセッションでは、バケットの内容がワークスペースの永続化から除外され、意図的に動的なマウント変更やセッション再開はサポートされません。

### 0.18.0

このマイナーリリースに破壊的変更は **ありません**。マイナーバージョンの更新は、Realtime エージェントのデフォルトモデル更新のみを目的としています。

主な変更点：

-   Realtime エージェントでは、デフォルトモデルとして `gpt-realtime-2.1` が使用されるようになり、新しい Realtime 設定では追加設定なしで最新の推奨モデルが使用されます。

### 0.17.0

このバージョンでは、サンドボックスのローカルソース実体化において、ソースパスが `Manifest.extra_path_grants` の対象でない限り、`LocalFile.src` と `LocalDir.src` が実体化用の `base_dir` 内に維持されます。`base_dir` は、マニフェスト適用時の SDK プロセスの現在の作業ディレクトリです。相対ローカルソースはそのディレクトリを基準に解決されますが、絶対ローカルソースは、すでにそのディレクトリ内にあるか、明示的な許可の対象である必要があります。これによりローカル成果物の境界に関する問題は解消されますが、そのベースディレクトリ外にある信頼済みのホストファイルまたはディレクトリを、意図的にサンドボックスワークスペースへコピーするアプリケーションには影響する可能性があります。

移行するには、マニフェストレベルで `SandboxPathGrant` を使用して信頼済みホストルートを許可してください。サンドボックスがそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることを推奨します。

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

`extra_path_grants` は、信頼済みのアプリケーション設定として扱ってください。アプリケーションですでに対象ホストパスを承認していない限り、モデル出力やその他の信頼できないマニフェスト入力から許可を設定しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` ではなく `gpt-5.4-mini` になりました。これは、モデルを明示的に設定していないエージェントと実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙的なデフォルトモデル設定に `reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルトが含まれるようになりました。

以前のデフォルトモデルの動作を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に指定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

主な変更点：

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` で、ターン制限を無効にする `max_turns=None` を受け付けるようになりました。
-   サンドボックスワークスペースのハイドレーションでは、ローカル、Docker、およびプロバイダー提供のサンドボックス実装全体で、絶対パスのシンボリックリンク先を含め、アーカイブルート外を指すシンボリックリンクを含む tar アーカイブを拒否するようになりました。

### 0.15.0

このバージョンでは、モデルによる拒否が空のテキスト出力として扱われたり、structured outputs の場合に実行ループが `MaxTurnsExceeded` まで再試行されたりするのではなく、`ModelRefusalError` として明示的に提示されるようになりました。

これは以前、拒否のみのモデルレスポンスが `final_output == ""` で完了することを期待していたコードに影響します。例外を発生させずに拒否を処理するには、`model_refusal` 実行エラーハンドラーを指定してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs を使用するエージェントでは、ハンドラーがエージェントの出力スキーマに一致する値を返すことができ、SDK は他の実行エラーハンドラーの最終出力と同様に検証します。

### 0.14.0

このマイナーリリースに破壊的変更は **ありません** が、サンドボックスエージェントという主要な新しいベータ機能領域に加え、ローカル、コンテナー化、ホスト環境全体で利用するために必要なランタイム、バックエンド、ドキュメントのサポートが追加されます。

主な変更点：

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しいベータ版サンドボックスランタイムの API サーフェスが追加されました。これによりエージェントは、ファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開機能を備えた永続的な隔離ワークスペース内で作業できます。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` によるローカルおよびコンテナー化された開発向けのサンドボックス実行バックエンドに加え、Python パッケージのオプション依存関係 extras を通じて、Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel のホスト型プロバイダー統合が追加されました。
-   サンドボックスメモリのサポートが追加され、段階的開示、複数ターンのグループ化、設定可能な分離境界、および S3 を利用したワークフローを含む永続化メモリのサンプルコードにより、今後の実行で過去の実行から得た知見を再利用できるようになりました。
-   ローカルおよび合成ワークスペースエントリ、S3／R2／GCS／Azure Blob Storage／S3 Files 用のリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットによる再開フローを含む、より広範なワークスペースおよび再開モデルが追加されました。
-   `examples/sandbox/` 配下に多数のサンドボックスのサンプルコードとチュートリアルが追加されました。スキル、ハンドオフ、メモリ、プロバイダー固有の設定を使用するコーディングタスク、およびコードレビュー、データルーム QA、Web サイトの複製などのエンドツーエンドワークフローを扱います。
-   サンドボックス対応のセッション準備、機能のバインディング、状態のシリアライズ、統合トレーシング、プロンプトキャッシュキーのデフォルト、および機密性の高い MCP 出力のより安全なリダクションにより、コアランタイムとトレーシングスタックが拡張されました。

### 0.13.0

このマイナーリリースに破壊的変更は **ありません** が、重要な Realtime のデフォルト更新に加え、新しい MCP 機能とランタイム安定性の修正が含まれます。

主な変更点：

-   デフォルトの WebSocket Realtime モデルは `gpt-realtime-1.5` になり、新しい Realtime エージェント設定では追加設定なしで新しいモデルが使用されます。
-   `MCPServer` で `list_resources()`、`list_resource_templates()`、`read_resource()` が公開され、`MCPServerStreamableHttp` で `session_id` が公開されるようになりました。これにより、MCP Streamable HTTP トランスポートを使用するセッションを、再接続またはステートレスワーカーをまたいで再開できます。
-   Chat Completions 統合では、`should_replay_reasoning_content` を使用して既存の推論内容を再送信するようオプトインできるようになり、LiteLLM／DeepSeek などのアダプターで、プロバイダー固有の推論とツール呼び出しの連続性が向上しました。
-   `SQLAlchemySession` での最初の書き込みの競合、推論除去後に孤立したアシスタントメッセージ ID を含むコンパクションリクエスト、MCP／推論項目を残す `remove_all_tools()`、`FunctionTool` インスタンス用バッチエグゼキューターの競合状態など、複数のランタイムおよびセッションのエッジケースが修正されました。

### 0.12.0

このマイナーリリースに破壊的変更は **ありません**。主な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースに破壊的変更は **ありません**。主な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースに破壊的変更は **ありません** が、OpenAI Responses ユーザー向けの重要な新機能領域として、Responses API の WebSocket トランスポート対応が含まれます。

主な変更点：

-   OpenAI Responses モデル向けの WebSocket トランスポート対応が追加されました（オプトイン方式であり、HTTP が引き続きデフォルトのトランスポートです）。
-   複数ターンの実行で、WebSocket 対応の共有プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー／`ResponsesWebSocketSession` が追加されました。
-   ストリーミング、ツール、承認、後続ターンを扱う、新しい WebSocket ストリーミングのサンプルコード（`examples/basic/stream_ws.py`）が追加されました。

### 0.9.0

このバージョンでは、Python 3.9 のサポートが終了しました。このメジャーバージョンは 3 か月前に EOL を迎えています。より新しいランタイムバージョンにアップグレードしてください。

また、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` に限定されました。通常、この変更が破壊的な問題を引き起こすことはありませんが、コードがより広い共用体型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、次の 2 つのランタイム動作変更により、移行作業が必要になる可能性があります。

- `FunctionTool` インスタンスでラップされた **同期** Python callable は、イベントループスレッドで実行されるのではなく、`asyncio.to_thread(...)` を通じてワーカースレッド上で実行されるようになりました。ツールロジックがスレッドローカル状態またはスレッドアフィンなリソースに依存している場合は、非同期ツール実装に移行するか、ツールコード内でスレッドアフィニティを明示してください。
- ローカル MCP ツールの失敗処理が設定可能になり、デフォルト動作では実行全体を失敗させる代わりに、モデルから参照可能なエラー出力を返せるようになりました。フェイルファストのセマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある動作変更がいくつかありました。

- ネストされたハンドオフ履歴は **オプトイン** になりました（デフォルトでは無効です）。v0.6.x のデフォルトのネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1`／`gpt-5.2` のデフォルトの `reasoning.effort` が、`"none"` に変更されました（以前は SDK のデフォルトで設定された `"low"` でした）。プロンプトまたは品質／コストプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴が、ユーザーとアシスタントのターンを個別のメッセージとして渡すのではなく、単一のアシスタントメッセージにまとめられるようになり、後続のエージェントに簡潔で予測可能な要約が提供されます。
- 既存の単一メッセージ形式のハンドオフ記録は、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に、正確なリテラルテキスト `For context, here is the conversation so far between the user and the previous agent:` を付けて開始するようになり、後続のエージェントは明確にラベル付けされた要約を受け取れます。

### 0.5.0

このバージョンには外部から確認できる破壊的変更はありませんが、内部には新機能といくつかの重要な更新が含まれています。

- `RealtimeRunner` に、[SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理するためのサポートが追加されました。
- Python 3.14 との互換性のため、`Runner#run_sync` の内部ロジックが大幅に改訂されました。

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK とともに openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API 対応が gpt-realtime モデルとその API インターフェース（GA 版）に移行します。

### 0.2.0

このバージョンでは、以前 `Agent` を引数として受け取っていたいくつかの箇所で、代わりに `AgentBase` を引数として受け取るようになりました。たとえば、これは MCP サーバーの `list_tools()` メソッドシグネチャに適用されます。これは純粋に型付けのみの変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正してください。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` の 2 つの新しいパラメーターが追加されました。`MCPServer` のサブクラスでオーバーライドされたすべての `MCPServer.list_tools()` メソッドに、これらのパラメーターを追加する必要があります。