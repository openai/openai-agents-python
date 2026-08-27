---
search:
  exclude: true
---
# リリースプロセス／変更履歴

このプロジェクトでは、`0.Y.Z` 形式を使用した、セマンティックバージョニングを一部変更した方式に従います。先頭の `0` は、SDK が現在も急速に進化していることを示します。各構成要素は次のように増分します。

## マイナー（`Y`）バージョン {#minor-y-versions}

ベータと明記されていない公開インターフェースに **破壊的変更** が加えられる場合、マイナーバージョン `Y` を増分します。たとえば、`0.0.x` から `0.1.x` への変更には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することをお勧めします。

## パッチ（`Z`）バージョン {#patch-z-versions}

非破壊的変更では `Z` を増分します。

-   バグ修正
-   新機能
-   非公開インターフェースへの変更
-   ベータ機能の更新

## 破壊的変更の履歴 {#breaking-change-changelog}

### 0.22.0 {#0220}

バージョン 0.22.0 では、既存の複数の API に対する失敗処理とデータ分離が強化されました。明示的なクライアントを指定して `OpenAIProvider` を構築し、さらにプロバイダーへ `organization` または `project` を渡しているアプリケーションでは、重複するこれらの引数を削除する必要があります。

主な変更点：

-   エージェントレベルの出力ガードレールが、終端関数ツールによって直接生成された最終出力をブロックした場合、検証済みフィールドによって安全に再構築できる場合に限り、SDK は再実行可能な呼び出し／出力ペアを保持します。元の `function_call_output` ペイロードは、セッション履歴、`RunState`、およびストリーミングされた実行結果の状態において、固定テキスト `"Output withheld by an output guardrail."` に置き換えられます。また、ペイロードを含む現在のレスポンスのガードレールメタデータは、消去または置換されます。現在のレスポンスに推論やその他の未対応形式が含まれる場合、SDK は代わりに現在のレスポンスの接尾部分全体を破棄します。以前に受理されたターンとガードレールの結果は引き続き利用できます。[出力ガードレール](guardrails.md#output-guardrails)を参照してください。
-   非ストリーミングの OpenAI Responses 呼び出しでは、返されたレスポンスの終端ステータスが `failed` または `incomplete` の場合、既存のストリーミング終端イベント処理と同様に `ModelBehaviorError` が送出されるようになりました。これは、`OpenAIResponsesModel` と `AnyLLMModel` の Responses 経路に適用されます。[例外](running_agents.md#exceptions)を参照してください。
-   [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider] は、`openai_client` と `organization` または `project` を組み合わせた場合にも `UserError` を送出するようになりました。既存の `api_key`、`base_url`、`websocket_base_url` との競合に変更はありません。代わりに、明示的な `AsyncOpenAI` クライアントでこれらの値を設定してください。[API キーとクライアント](config.md#api-keys-and-clients)を参照してください。
-   各 `RunResult.to_state()` チェックポイントが、独立した使用量スナップショットを保持するようになりました。再開された実行結果はチェックポイントの合計値から始まり、元の実行結果や同階層のチェックポイントを変更することなく、それ自体のモデル呼び出しを加算します。ネストされた `Agent.as_tool()` の再開では、再開後の使用量が引き続きアクティブな外側の実行に集約されます。[RunState チェックポイントでの使用量](usage.md#usage-in-runstate-checkpoints)を参照してください。
-   エージェントの可視化では、`handoff(agent)` で登録された対象のツール、MCP サーバー、および後続のハンドオフが再帰的に展開されるようになりました。これは、エージェントの `handoffs` リスト内の直接的な `Agent` エントリと同じ動作です。[グラフの生成](visualization.md#generating-a-graph)を参照してください。
-   `Agent.clone()` および `RealtimeAgent.clone()` の API ガイダンスでは、既存のシャローコピー動作を正確に説明するようになりました。オーバーライドされていないリスト属性は、同じリストオブジェクトのままです。クローンがコンテナーを独立して所有する必要がある場合は、新しいリストを渡してください。[エージェントのクローン／コピー](agents.md#cloningcopying-agents)を参照してください。

### 0.21.0 {#0210}

バージョン 0.21.0 では `openai` v3 が必須となり、Agents SDK の OpenAI HTTP 統合が HTTPX2 に移行されました。デフォルトの OpenAI クライアントを使用するアプリケーションではクライアント設定を変更する必要はありませんが、OpenAI HTTP レイヤーをカスタマイズしているアプリケーションでは、トランスポート関連コードの移行が必要になる場合があります。

主な変更点：

-   必須の OpenAI 依存関係は `openai>=3.0.0,<4` になりました。クリーンなコアインストールでは HTTPX2 が使用され、従来の `httpx` は直接依存関係としてインストールされなくなりました。
-   デフォルトの OpenAI プロバイダー、音声プロバイダー、Responses WebSocket サポート、トレーシングエクスポーター、およびプロバイダーのリトライ正規化で HTTPX2 が使用されるようになりました。既存の Agents SDK の公開設定と実行時動作に変更はありません。
-   `AsyncOpenAI` に `http_client=` を渡すアプリケーションでは、カスタムクライアント、トランスポート、認証、イベントフック、モックトランスポート、タイムアウト値、URL、リクエスト、レスポンス、およびトランスポート例外処理を `httpx` から `httpx2` へ移行してください。OpenAI クライアントのデフォルト設定とカスタム HTTP オプションの両方が必要な場合は、OpenAI Python SDK の `DefaultAsyncHttpx2Client` を使用することを推奨します。[`openai` v3 でのカスタム HTTP クライアント](config.md#custom-http-clients-with-openai-v3)を参照してください。
-   Agents SDK は、従来の任意の HTTPX オブジェクトを HTTPX2 に変換しません。OpenAI Python SDK の一時的なレガシークライアント互換経路では、`httpx` を明示的にインストールする必要があり、移行のための橋渡しとして扱う必要があります。
-   ローカル MCP の HTTP カスタマイズでは、引き続きインストール済みの MCP パッケージに従います。MCP Python SDK v1 は従来の `httpx` を提供して使用し、MCP Python SDK v2 は `httpx2` を使用します。通常の MCP 接続では、アプリケーションを変更する必要はありません。[MCP Python SDK v1 および v2](mcp.md#mcp-python-sdk-v1-and-v2)を参照してください。
-   公開されたプロバイダー非依存のテストユーティリティで、プロバイダーやプロセスへの依存なしに、エージェントモデル、サンドボックスセッション、Realtime セッション、および音声パイプラインのワークフローを扱えるようになりました。実際のプロバイダーアダプターまたは統合境界を維持すべき場合のレシピとガイダンスについては、[テスト](testing.md)を参照してください。

### 0.20.0 {#0200}

バージョン 0.20.0 には、ローカル MCP HTTP トランスポートをカスタマイズするアプリケーションにとって破壊的となる可能性がある MCP 依存関係の移行が含まれます。また、エージェントまたは実行でモデルを明示的に選択しない場合に使用される SDK のデフォルトモデルも更新されました。

主な変更点：

-   SDK のデフォルトモデルは、`gpt-5.4-mini` から `gpt-5.6-luna` に変更されました。デフォルトの `reasoning.effort="none"` および `verbosity="low"` 設定に変更はありません。
-   明示的なエージェントモデル、実行レベルのモデルオーバーライド、および `OPENAI_DEFAULT_MODEL` 環境変数は、引き続き SDK のデフォルトより優先されます。
-   Realtime 入力文字起こし設定で、`gpt-transcribe`、`gpt-live-transcribe`、`gpt-realtime-whisper` が認識されるようになりました。低レイテンシーの `gpt-live-transcribe` セッションでは、ネストされた `audio.input.transcription` 設定で `prompt`、`keywords`、および複数の想定 `languages` を指定できます。この SDK が固定している OpenAI クライアントのバージョンでは、`delay` のレイテンシー／精度レベルは `gpt-realtime-whisper` でのみサポートされます。確定済みの音声ターン後の文字起こし、または検出言語の出力には、WebSocket 経由で `gpt-transcribe` を使用してください。`audio.input.turn_detection=None` を明示的に設定すると、自動ターン検出が無効になります。[入力文字起こし設定](realtime/guide.md#input-transcription-settings)を参照してください。
-   Agents SDK によって作成されるローカル MCP 接続では、`mcp>=1.19.0,<3` による v1 互換性を維持しながら、MCP Python SDK v2 がサポートされるようになりました。Agents SDK は通常の stdio、SSE、および Streamable HTTP 接続を自動的に適応させます。MCP v2 がインストールされている場合、これらの接続は `mcp.Client(mode="auto")` を使用してサポート対象の最新プロトコルを探索し、古いサーバーでは従来の `initialize` ハンドシェイクへフォールバックします。依存関係の解決で MCP v2 が選択された場合、カスタム `httpx.Auth` オブジェクトまたは `httpx.AsyncClient` ファクトリーを提供するアプリケーションでは、それらの値を `httpx2` に移行するか、v1 HTTP スタックを維持するために `mcp<2` に固定する必要があります。`MCPServerStreamableHttp` の `params["ignore_initialized_notification_failure"] = True` オプションも引き続き v1 専用です。移行の詳細については、[MCP Python SDK v1 および v2](mcp.md#mcp-python-sdk-v1-and-v2)を参照してください。
-   サンドボックスのマウント検証では、サンドボックスまたはマウントヘルパーの副作用が発生する前に、安全でない認証情報の配置を拒否するようになりました。信頼済みアプリケーションでは、ストレージ機能テーブルを変更せずに、コンテナー内の正確なマウントパスに対するマウント範囲または広範な認証情報の公開を承認できます。これらの承認は実行時にのみ有効であり、シリアライズされたサンドボックス状態自体が認証情報への権限を付与することはありません。保護されたマウント境界では、SDK は新しい秘匿化済み例外を返します。元の例外が正確に認識された SDK サンドボックスエラーであり、承認済みの構造化フィールドが検証に合格した場合、置換後もそのサブタイプと検証済みの安全なフィールドが維持されます。認識された `MountConfigError` では、SDK が生成した安全な検証メッセージも維持できます。それ以外の場合、SDK は新しい汎用の秘匿化済みエラーを返します。プロバイダーによって制御されるメッセージや、その他の未承認のメッセージ、コマンドデータ、注記、コンテキスト、原因、および元のトレースバック状態は保持されません。[マウントとリモートストレージ](sandbox/clients.md#mounts-and-remote-storage)および[セッション状態からの再開](sandbox/guide.md#resume-from-session-state)を参照してください。
-   リトライポリシーでは、安定した再実行安全性の情報を確認し、プロバイダーが安全でないと判断した非ストリーミングリクエストに対して `RetryDecision(approve_unsafe_replay=True)` を明示的に設定できます。この承認によって、中止、送出済みのストリーミング出力、または Programmatic Tool Calling などの別個のローカル副作用拒否を回避することはできません。[Runner が管理するリトライ](models/index.md#runner-managed-retries)を参照してください。
-   再開可能な `RunState` オブジェクトでは、次回のモデル呼び出し前に `add_input()` を使用して永続的なユーザー入力を準備できるようになりました。準備された入力はシリアライズ後も保持され、入力ガードレールを通過し、ローカルセッションおよびサーバー管理の会話全体で永続的な SDK 入力を 1 回生成します。安全でない再実行が明示的に承認されている場合、入力がプロバイダーへ再送信され、プロバイダー側の処理が繰り返される可能性があります。[再開前の入力追加](results.md#add-input-before-resuming)を参照してください。
-   実行時の信頼性修正により、ストリーミングと非ストリーミングの[出力ガードレールのセッション永続化](guardrails.md#output-guardrails)が統一され、コピーおよび名前空間化の際に `FunctionTool` のサブクラスが維持されるようになりました。また、[未対応の Chat Completions 音声出力](models/index.md#chat-completions-compatibility-options)では、空のストリームを暗黙的に完了する代わりに、明示的なエラーが送出されるようになりました。`OpenAIResponsesCompactionSession` ラッパーは、キャンセルが呼び出し元へ到達する前に、[コンパクション前の履歴復元](sessions/index.md#auto-compaction-can-block-streaming)を試行して完了を待ちます。[`VoicePipeline`](voice/pipeline.md#results) のコンシューマーは、正常な実行後に文字起こしセッションのクローズが失敗した場合、その失敗を受け取るようになりました。一方、先に発生したターンの失敗は、後から発生したクローズの失敗より優先されます。`RunState` のラウンドトリップでは、ローカルシェル出力、承認済みのコンピューター安全性チェック、デフォルト値のツール出力フィールド、および辞書、リスト、タプルの走査中に検出された Pydantic モデルまたは dataclass の出力が維持されるようになりました。MCP 変換では、自由形式のオブジェクトスキーマと画像出力が維持され、音声ブロックやリソースブロックなど、その他の raw コンテンツブロックは有効な JSON テキストとしてシリアライズされます。`MCPServerManager` は、重複するライフサイクル操作を直列化し、接続とクリーンアップに有限のデフォルトタイムアウトを適用します。モデルの再実行では、出力項目を入力として使用する前に、サーバー所有の `created_by` メタデータが削除されます。

### 0.19.0 {#0190}

このマイナーリリースでは、破壊的変更は導入されて **いません**。マイナーバージョンの増分は、OpenAI Responses の重要な新機能領域である Programmatic Tool Calling を反映したものです。

主な変更点：

-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] が追加されました。これにより、対応する OpenAI Responses モデルは JavaScript を生成し、Programmatic Tool Calling の対象となるツールを連携させることができます。ツール単位の `allowed_callers`、`FunctionTool` インスタンスからの structured outputs、Runner のストリーミング、ガードレール、承認、セッション、および `RunState` との統合をサポートします。設定と制約については、[Programmatic Tool Calling](tools.md#programmatic-tool-calling)を参照してください。
-   公開 `agents.decorators` モジュール、および既存のガードレールデコレーターとともに、既存の `@function_tool` デコレーターの短い別名として `@tool` が追加されました。`FunctionTool` インスタンスでは、非同期呼び出し可能オブジェクトもサポートされるようになりました。
-   SDK 設定では、エージェント、実行、モデル、セッション、サンドボックス、音声パイプラインの全体で、型付き設定オブジェクトまたは辞書のいずれかを一貫して受け入れるようになり、不明な設定も検証されます。
-   モデル、ツール、MCP、Realtime、セッション、サンドボックス、およびトレーシング全体のエラーと診断ログが強化され、有用なデバッグコンテキストを維持しながら、raw の機密ペイロードが公開されないようになりました。
-   AnyLLM、LiteLLM、および Chat Completions の互換性が向上し、モデルのリトライ間でセッション履歴が維持されるようになりました。また、レスポンス開始前に発生した WebSocket の過負荷に関するプロバイダーのリトライガイダンスが追加され、オプトインした Runner のリトライポリシーで、許可されている場合に失敗した試行を再実行できるようになりました。
-   `VercelCloudBucketMountStrategy` を通じて、[Vercel サンドボックスの作成時にのみ設定できる S3 マウント](sandbox/clients.md#mounts-and-remote-storage)が追加されました。マウントされたセッションでは、バケットの内容がワークスペースの永続化から除外され、動的なマウント変更やセッション再開は意図的にサポートされません。

### 0.18.0 {#0180}

このマイナーリリースでは、破壊的変更は導入されて **いません**。マイナーバージョンの増分は、Realtime エージェントのデフォルトモデル更新のみを目的としています。

主な変更点：

-   Realtime エージェントのデフォルトモデルとして `gpt-realtime-2.1` が使用されるようになり、新しい Realtime 設定では追加設定なしで最新の推奨モデルが使用されます。

### 0.17.0 {#0170}

このバージョンでは、ソースパスが `Manifest.extra_path_grants` の対象でない限り、サンドボックスのローカルソースの実体化において、`LocalFile.src` と `LocalDir.src` が実体化の `base_dir` 内に保持されます。`base_dir` は、マニフェストが適用される時点での SDK プロセスの現在の作業ディレクトリです。相対ローカルソースはそのディレクトリを基準に解決されますが、絶対ローカルソースは、すでにそのディレクトリ内または明示的に許可された範囲内に存在する必要があります。これによりローカルアーティファクトの境界に関する問題は解消されますが、信頼済みホストのファイルまたはディレクトリを、そのベースディレクトリの外部からサンドボックスワークスペースへ意図的にコピーするアプリケーションに影響する可能性があります。

移行するには、マニフェストレベルで `SandboxPathGrant` を使用して信頼済みホストのルートを許可してください。サンドボックスでそれらのファイルを読み取るだけの場合は、読み取り専用にすることを推奨します。

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

`extra_path_grants` は、信頼済みアプリケーション設定として扱ってください。アプリケーションが対象のホストパスを事前に承認していない限り、モデル出力やその他の信頼できないマニフェスト入力から許可設定を作成しないでください。

### 0.16.0 {#0160}

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` から `gpt-5.4-mini` に変更されました。これは、モデルを明示的に設定していないエージェントと実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙的なデフォルトモデル設定には、`reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルトが含まれるようになりました。

以前のデフォルトモデルの動作を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に指定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

主な変更点：

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` では、ターン制限を無効にするための `max_turns=None` を受け入れるようになりました。
-   サンドボックスワークスペースのハイドレーションでは、ローカル、Docker、プロバイダー対応の各サンドボックス実装において、絶対シンボリックリンク先を含め、アーカイブルートの外部を指すシンボリックリンクを含む tar アーカイブを拒否するようになりました。

### 0.15.0 {#0150}

このバージョンでは、モデルによる拒否が空のテキスト出力として扱われたり、structured outputs の場合に実行ループが `MaxTurnsExceeded` までリトライされたりする代わりに、`ModelRefusalError` として明示的に提示されるようになりました。

これは、拒否のみのモデルレスポンスが `final_output == ""` で完了することを以前に想定していたコードに影響します。例外を送出せずに拒否を処理するには、`model_refusal` 実行エラーハンドラーを指定してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs を使用するエージェントでは、ハンドラーはエージェントの出力スキーマに一致する値を返すことができ、SDK は他の実行エラーハンドラーの最終出力と同様にその値を検証します。

### 0.14.0 {#0140}

このマイナーリリースでは、破壊的変更は導入されて **いません**。ただし、主要な新しいベータ機能領域であるサンドボックスエージェントに加え、ローカル、コンテナー化、ホスト環境で使用するために必要なランタイム、バックエンド、ドキュメントのサポートが追加されています。

主な変更点：

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しいベータ版サンドボックスランタイムインターフェースが追加され、エージェントがファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開サポートを備えた永続的な分離ワークスペース内で作業できるようになりました。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` を通じたローカルおよびコンテナー化された開発向けのサンドボックス実行バックエンドに加え、Python パッケージのオプション依存関係 extras を通じて、Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel 向けのホスト型プロバイダー統合が追加されました。
-   サンドボックスのメモリサポートが追加され、段階的開示、複数ターンのグループ化、設定可能な分離境界、S3 を利用したワークフローを含む永続メモリのコード例により、将来の実行で以前の実行から得た知見を再利用できるようになりました。
-   ローカルおよび合成ワークスペースエントリ、S3／R2／GCS／Azure Blob Storage／S3 Files 向けのリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットを使用する再開フローを含む、より包括的なワークスペースおよび再開モデルが追加されました。
-   `examples/sandbox/` 配下に、多数のサンドボックスのコード例とチュートリアルが追加されました。スキル、ハンドオフ、メモリを使用するコーディングタスク、プロバイダー固有の設定、コードレビュー、データルーム QA、Web サイトのクローン作成などのエンドツーエンドワークフローを扱います。
-   サンドボックス対応のセッション準備、機能のバインド、状態のシリアライズ、統合トレーシング、プロンプトキャッシュキーデフォルト、および機密性の高い MCP 出力のより安全な秘匿化により、コアランタイムとトレーシングスタックが拡張されました。

### 0.13.0 {#0130}

このマイナーリリースでは、破壊的変更は導入されて **いません**。ただし、注目すべき Realtime のデフォルト更新、新しい MCP 機能、およびランタイムの安定性修正が含まれます。

主な変更点：

-   デフォルトの WebSocket Realtime モデルが `gpt-realtime-1.5` になり、新しい Realtime エージェント設定では追加設定なしで新しいモデルが使用されます。
-   `MCPServer` では `list_resources()`、`list_resource_templates()`、`read_resource()` が公開され、`MCPServerStreamableHttp` では `session_id` が公開されるようになりました。これにより、MCP Streamable HTTP トランスポートを使用するセッションを、再接続やステートレスワーカーをまたいで再開できます。
-   Chat Completions 統合では、`should_replay_reasoning_content` を使用して既存の推論内容を再送信することをオプトインできるようになり、LiteLLM／DeepSeek などのアダプターで、プロバイダー固有の推論およびツール呼び出しの連続性が向上しました。
-   `SQLAlchemySession` での同時初回書き込み、推論除去後に孤立した assistant メッセージ ID を含むコンパクションリクエスト、MCP／推論項目を残す `remove_all_tools()`、`FunctionTool` インスタンス向けバッチエグゼキューターの競合状態など、複数のランタイムおよびセッションのエッジケースが修正されました。

### 0.12.0 {#0120}

このマイナーリリースでは、破壊的変更は導入されて **いません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0 {#0110}

このマイナーリリースでは、破壊的変更は導入されて **いません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0 {#0100}

このマイナーリリースでは、破壊的変更は導入されて **いません**。ただし、OpenAI Responses のユーザー向けに、Responses API の WebSocket トランスポートサポートという重要な新機能領域が含まれます。

主な変更点：

-   OpenAI Responses モデル向けの WebSocket トランスポートサポートが追加されました（オプトイン方式であり、HTTP は引き続きデフォルトのトランスポートです）。
-   複数ターンの実行にわたって、WebSocket 対応の共有プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー／`ResponsesWebSocketSession` が追加されました。
-   ストリーミング、ツール、承認、後続ターンを扱う新しい WebSocket ストリーミングのコード例（`examples/basic/stream_ws.py`）が追加されました。

### 0.9.0 {#090}

このバージョンでは、このメジャーバージョンが 3 か月前に EOL を迎えたため、Python 3.9 はサポートされなくなりました。より新しいランタイムバージョンへアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` に絞り込まれました。通常、この変更によって破壊的な問題が発生することはありませんが、コードがより広いユニオン型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0 {#080}

このバージョンでは、2 つのランタイム動作の変更により、移行作業が必要になる場合があります。

- `FunctionTool` インスタンスがラップする **同期** Python callable は、イベントループスレッド上で実行される代わりに、`asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールロジックがスレッドローカル状態またはスレッドアフィニティを持つリソースに依存している場合は、非同期ツール実装へ移行するか、ツールコード内でスレッドアフィニティを明示してください。
- ローカル MCP ツールの失敗処理が設定可能になり、デフォルト動作では実行全体を失敗させる代わりに、モデルから参照可能なエラー出力を返せるようになりました。即時失敗のセマンティクスに依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベルの設定をオーバーライドするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0 {#070}

このバージョンでは、既存のアプリケーションに影響する可能性がある動作変更がいくつかあります。

- ネストされたハンドオフ履歴は、**オプトイン** 方式（デフォルトでは無効）になりました。v0.6.x のデフォルトのネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1`／`gpt-5.2` のデフォルトの `reasoning.effort` が、SDK のデフォルトで設定されていた以前のデフォルト `"low"` から `"none"` に変更されました。プロンプトまたは品質／コストの特性が `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0 {#060}

このバージョンでは、ユーザーと assistant の各ターンを別々のメッセージとして渡す代わりに、デフォルトのハンドオフ履歴が単一の assistant メッセージにまとめられるようになり、後続のエージェントに簡潔で予測可能な要約が提供されます
- 既存の単一メッセージによるハンドオフのトランスクリプトは、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に正確なリテラルテキスト `For context, here is the conversation so far between the user and the previous agent:` から始まるようになり、後続のエージェントに明確なラベル付きの要約が提供されます

### 0.5.0 {#050}

このバージョンでは、目に見える破壊的変更は導入されていませんが、新機能と内部の重要な更新がいくつか含まれています。

- `RealtimeRunner` に、[SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理するためのサポートが追加されました。
- Python 3.14 との互換性のため、`Runner#run_sync` の内部ロジックが大幅に改訂されました

### 0.4.0 {#040}

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK とともに openai v2.x を使用してください。

### 0.3.0 {#030}

このバージョンでは、Realtime API のサポートが gpt-realtime モデルとその API インターフェース（GA バージョン）へ移行します。

### 0.2.0 {#020}

このバージョンでは、以前は引数として `Agent` を受け取っていた箇所の一部が、代わりに `AgentBase` を受け取るようになりました。たとえば、これは MCP サーバーの `list_tools()` メソッドシグネチャに適用されます。これは型指定のみの変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正してください。

### 0.1.0 {#010}

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されました。`MCPServer` のサブクラスでオーバーライドされているすべての `MCPServer.list_tools()` メソッドに、これらのパラメーターを追加する必要があります。