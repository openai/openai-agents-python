---
search:
  exclude: true
---
# リリースプロセス／変更履歴

このプロジェクトでは、`0.Y.Z` 形式のセマンティックバージョニングを一部変更して使用しています。先頭の `0` は、SDK が現在も急速に進化していることを示します。各要素は次のように更新します。

## マイナー (`Y`) バージョン

ベータと明記されていない公開インターフェースに **破壊的変更** がある場合、マイナーバージョン `Y` を上げます。たとえば、`0.0.x` から `0.1.x` への変更には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することを推奨します。

## パッチ (`Z`) バージョン

破壊的でない変更では、`Z` を上げます。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## 破壊的変更の変更履歴

### 0.19.0

このマイナーリリースでは、破壊的変更を **導入していません** 。マイナーバージョンの更新は、OpenAI Responses の重要な新機能領域であるプログラムによるツール呼び出しを反映したものです。

主な変更点:

-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] を追加しました。これにより、対応する OpenAI Responses モデルは、利用可能なツールを連携させる JavaScript を生成できます。ツールごとの `allowed_callers`、関数ツールの構造化された出力、Runner のストリーミング、ガードレール、承認、セッション、`RunState` との統合をサポートします。設定と制約については、[プログラムによるツール呼び出し](tools.md#programmatic-tool-calling)を参照してください。
-   公開モジュール `agents.decorators` と、既存の関数およびガードレール用デコレーターに加えて短い別名 `@tool` を追加しました。関数ツールで非同期 callable オブジェクトもサポートするようになりました。
-   エージェント、実行、モデル、セッション、サンドボックス、音声パイプラインの SDK 設定で、型付き設定オブジェクトまたは辞書を一貫して受け付けるようになり、不明な設定も検証されます。
-   モデル、ツール、MCP、Realtime、セッション、サンドボックス、トレーシングのエラーおよび診断ログを強化し、有用なデバッグ情報を維持しながら、機密性の高い生のペイロードが露出しないようにしました。
-   AnyLLM、LiteLLM、Chat Completions との互換性を改善し、モデルの再試行時にセッション履歴を維持するとともに、レスポンス開始前に発生する WebSocket の過負荷エラーを再試行するようにしました。
-   `VercelCloudBucketMountStrategy` を使用した、[Vercel サンドボックス向けの作成時限定 S3 マウント](sandbox/clients.md#mounts-and-remote-storage)を追加しました。マウントを含むセッションでは、ワークスペースの永続化からバケットの内容が除外され、動的なマウント変更やセッションの再開は意図的にサポートされません。

### 0.18.0

このマイナーリリースでは、破壊的変更を **導入していません** 。マイナーバージョンの更新は、Realtime エージェントのデフォルトモデル更新のみを反映したものです。

主な変更点:

-   Realtime エージェントのデフォルトモデルが `gpt-realtime-2.1` になり、新しい Realtime 設定で追加構成なしに最新の推奨モデルが使用されるようになりました。

### 0.17.0

このバージョンでは、ソースパスが `Manifest.extra_path_grants` の対象でない限り、サンドボックスでローカルソースを実体化する際に `LocalFile.src` と `LocalDir.src` が実体化先の `base_dir` 内に維持されます。`base_dir` は、マニフェストが適用された時点における SDK プロセスの現在の作業ディレクトリです。相対パスのローカルソースはそのディレクトリを基準に解決されますが、絶対パスのローカルソースは、あらかじめそのディレクトリ内または明示的に許可されたパス内に存在する必要があります。これによりローカルアーティファクトの境界に関する問題は解消されますが、信頼済みのホストファイルまたはディレクトリを、そのベースディレクトリの外部からサンドボックスワークスペースへ意図的にコピーするアプリケーションには影響する可能性があります。

移行するには、マニフェストレベルで `SandboxPathGrant` を使用して信頼済みのホストルートを許可してください。サンドボックスがそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることを推奨します。

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

`extra_path_grants` は、信頼済みのアプリケーション設定として扱ってください。アプリケーションが対象のホストパスを事前に承認していない限り、モデル出力やその他の信頼できないマニフェスト入力から許可設定を作成しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` から `gpt-5.4-mini` に変更されました。これは、モデルを明示的に設定していないエージェントと実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙のデフォルトモデル設定に `reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルト設定が含まれるようになりました。

以前のデフォルトモデルの動作を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に指定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

主な変更点:

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` で `max_turns=None` を指定し、ターン数の制限を無効化できるようになりました。
-   サンドボックスワークスペースのハイドレーションでは、ローカル、Docker、およびプロバイダーを利用するすべてのサンドボックス実装において、絶対パスをリンク先とするシンボリックリンクを含め、アーカイブルートの外部を指すシンボリックリンクを含む tar アーカイブが拒否されるようになりました。

### 0.15.0

このバージョンでは、モデルの拒否応答が空のテキスト出力として扱われたり、structured outputs の場合に `MaxTurnsExceeded` に達するまで実行ループが再試行されたりするのではなく、`ModelRefusalError` として明示的に通知されるようになりました。

これは、拒否応答のみを含むモデルレスポンスが以前は `final_output == ""` で完了すると想定していたコードに影響します。例外を発生させずに拒否応答を処理するには、`model_refusal` 実行エラーハンドラーを指定してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs エージェントの場合、ハンドラーはエージェントの出力スキーマに一致する値を返すことができ、SDK は他の実行エラーハンドラーの最終出力と同様にその値を検証します。

### 0.14.0

このマイナーリリースでは、破壊的変更を **導入していません** 。ただし、サンドボックスエージェントという大規模な新しいベータ機能領域に加え、ローカル環境、コンテナ環境、ホスト環境でそれらを使用するために必要なランタイム、バックエンド、ドキュメントのサポートを追加しています。

主な変更点:

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しいベータ版サンドボックスランタイムインターフェースを追加しました。これにより、エージェントはファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開サポートを備えた、永続的で隔離されたワークスペース内で作業できます。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` を使用するローカルおよびコンテナ化された開発向けのサンドボックス実行バックエンドに加え、オプションの追加依存関係を通じて、Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel のホスト型プロバイダー統合を追加しました。
-   将来の実行で過去の実行から得た知見を再利用できるようにするサンドボックスメモリのサポートを追加しました。段階的開示、マルチターンのグループ化、構成可能な分離境界に加え、S3 を利用するワークフローを含む永続化メモリのコード例も提供します。
-   ローカルおよび合成ワークスペースエントリー、S3/R2/GCS/Azure Blob Storage/S3 Files 向けのリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットを使用する再開フローを含む、より包括的なワークスペースおよび再開モデルを追加しました。
-   `examples/sandbox/` 配下に、スキル、ハンドオフ、メモリを使用するコーディングタスク、プロバイダー固有の設定、コードレビュー、データルーム QA、Web サイトの複製などのエンドツーエンドのワークフローを扱う、多数のサンドボックス用コード例とチュートリアルを追加しました。
-   サンドボックスを考慮したセッション準備、機能のバインディング、状態のシリアライズ、統合トレーシング、プロンプトキャッシュキーのデフォルト設定、機密性の高い MCP 出力をより安全に秘匿する機能により、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースでは、破壊的変更を **導入していません** 。ただし、Realtime のデフォルト設定に関する重要な更新、新しい MCP 機能、ランタイムの安定性に関する修正が含まれています。

主な変更点:

-   WebSocket 用のデフォルト Realtime モデルが `gpt-realtime-1.5` になり、新しい Realtime エージェント設定で追加構成なしに新しいモデルが使用されるようになりました。
-   `MCPServer` で `list_resources()`、`list_resource_templates()`、`read_resource()` が公開されるようになりました。また、`MCPServerStreamableHttp` で `session_id` が公開されるようになり、ストリーミング可能な HTTP セッションを再接続後またはステートレスワーカー間で再開できるようになりました。
-   Chat Completions 統合では、`should_replay_reasoning_content` を使用して推論内容の再生を任意で有効化できるようになり、LiteLLM/DeepSeek などのアダプターで、プロバイダー固有の推論／ツール呼び出しの連続性が向上しました。
-   `SQLAlchemySession` への同時初回書き込み、推論内容の除去後に孤立したアシスタントメッセージ ID を含む圧縮リクエスト、`remove_all_tools()` の実行後に残る MCP／推論項目、関数ツールのバッチ実行処理における競合状態など、複数のランタイムおよびセッションのエッジケースを修正しました。

### 0.12.0

このマイナーリリースでは、破壊的変更を **導入していません** 。主な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースでは、破壊的変更を **導入していません** 。主な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースでは、破壊的変更を **導入していません** 。ただし、OpenAI Responses のユーザー向けに、Responses API の WebSocket トランスポートをサポートする重要な新機能領域が含まれています。

主な変更点:

-   OpenAI Responses モデルに WebSocket トランスポートのサポートを追加しました。これはオプトインであり、HTTP が引き続きデフォルトのトランスポートです。
-   複数ターンの実行で、WebSocket 対応の共有プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー／`ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、後続ターンを扱う新しい WebSocket ストリーミングのコード例 (`examples/basic/stream_ws.py`) を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 のメジャーバージョンが 3 か月前に EOL を迎えたため、Python 3.9 はサポートされなくなりました。より新しいランタイムバージョンにアップグレードしてください。

さらに、`Agent#as_tool()` メソッドの戻り値に対する型ヒントが、`Tool` から `FunctionTool` に限定されました。通常、この変更が破壊的な問題を引き起こすことはありませんが、コードがより広範なユニオン型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、2 つのランタイム動作の変更により、移行作業が必要になる可能性があります。

- Python の **同期** 呼び出し可能オブジェクトをラップする関数ツールは、イベントループのスレッド上で実行されるのではなく、`asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールのロジックがスレッドローカルな状態または特定のスレッドに依存するリソースを使用している場合は、非同期ツール実装に移行するか、ツールコード内でスレッドアフィニティを明示してください。
- ローカル MCP ツールの失敗処理を構成できるようになり、デフォルト動作では実行全体を失敗させる代わりに、モデルから参照可能なエラー出力を返す場合があります。即時失敗の動作に依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` 値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある動作変更がいくつかあります。

- ネストされたハンドオフ履歴が **オプトイン** になりました。デフォルトでは無効です。v0.6.x のデフォルトのネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
- `gpt-5.1`／`gpt-5.2` のデフォルトの `reasoning.effort` が、SDK のデフォルト設定で以前使用されていた `"low"` から `"none"` に変更されました。プロンプトまたは品質／コストのプロファイルが `"low"` に依存していた場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴が、未加工のユーザー／アシスタントのターンを公開する代わりに、単一のアシスタントメッセージへまとめられるようになり、後続のエージェントに簡潔で予測可能な要約を提供します
- 既存の単一メッセージ形式のハンドオフトランスクリプトは、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" という文言を付けて開始するようになり、後続のエージェントが明確なラベル付きの要約を受け取れるようになりました

### 0.5.0

このバージョンでは、外部から確認できる破壊的変更はありませんが、新機能と内部実装上の重要な更新がいくつか含まれています。

- `RealtimeRunner` で [SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理するためのサポートを追加しました
- Python 3.14 との互換性を確保するため、`Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x 系はサポートされなくなりました。この SDK とともに openai v2.x 系を使用してください。

### 0.3.0

このバージョンでは、Realtime API のサポートが gpt-realtime モデルとその API インターフェース（GA 版）へ移行します。

### 0.2.0

このバージョンでは、以前は `Agent` を引数として受け取っていた一部の箇所が、代わりに `AgentBase` を引数として受け取るようになりました。たとえば、MCP サーバーの `list_tools()` 呼び出しが該当します。これは型に関する変更のみであり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正してください。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されました。`MCPServer` を継承するすべてのクラスに、これらのパラメーターを追加する必要があります。
