---
search:
  exclude: true
---
# リリースプロセス／変更履歴

このプロジェクトでは、`0.Y.Z` 形式を使用した、セマンティックバージョニングをわずかに変更した方式に従います。先頭の `0` は、SDK が依然として急速に進化していることを示します。各構成要素は次のように更新します。

## マイナー（`Y`）バージョン

ベータと明記されていない公開インターフェースに **破壊的変更** がある場合、マイナーバージョン `Y` を増やします。たとえば、`0.0.x` から `0.1.x` への移行には、破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトでバージョンを `0.0.x` に固定することを推奨します。

## パッチ（`Z`）バージョン

破壊的でない変更の場合は、`Z` を増やします。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## 破壊的変更履歴

### 0.18.0

このマイナーリリースには、破壊的変更は **ありません**。マイナーバージョンの更新は、Realtime エージェントのデフォルトモデル更新のみを目的としています。

主な変更点：

-   Realtime エージェントのデフォルトモデルが `gpt-realtime-2.1` になり、新しい Realtime セットアップでは追加の設定なしで最新の推奨モデルが使用されるようになりました。

### 0.17.0

このバージョンでは、サンドボックスでローカルソースを実体化する際、ソースパスが `Manifest.extra_path_grants` の対象でない限り、`LocalFile.src` と `LocalDir.src` は実体化先の `base_dir` 内に保持されます。`base_dir` は、マニフェストの適用時点における SDK プロセスの現在の作業ディレクトリです。相対パスのローカルソースはそのディレクトリを基準に解決され、絶対パスのローカルソースは、すでにそのディレクトリ内にあるか、明示的な許可の対象である必要があります。これにより、ローカルアーティファクトの境界に関する問題が解消されますが、そのベースディレクトリ外にある信頼済みのホストファイルやディレクトリを意図的にサンドボックスワークスペースへコピーするアプリケーションには影響する可能性があります。

移行するには、`SandboxPathGrant` を使用してマニフェストレベルで信頼済みのホストルートを許可してください。サンドボックスがそれらのファイルを読み取るだけでよい場合は、読み取り専用にすることを推奨します。

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

`extra_path_grants` は、信頼済みのアプリケーション設定として扱ってください。アプリケーションが対象のホストパスを事前に承認していない限り、モデル出力やその他の信頼できないマニフェスト入力から許可設定を追加しないでください。

### 0.16.0

このバージョンでは、SDK のデフォルトモデルが `gpt-4.1` から `gpt-5.4-mini` に変更されました。これは、モデルを明示的に設定していないエージェントと実行に影響します。新しいデフォルトは GPT-5 モデルであるため、暗黙的なデフォルトモデル設定には、`reasoning.effort="none"` や `verbosity="low"` などの GPT-5 のデフォルト設定が含まれるようになりました。

以前のデフォルトモデルの動作を維持する必要がある場合は、エージェントまたは実行設定でモデルを明示的に指定するか、`OPENAI_DEFAULT_MODEL` 環境変数を設定してください。

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

主な変更点：

-   `Runner.run`、`Runner.run_sync`、`Runner.run_streamed` で `max_turns=None` を指定し、ターン数の上限を無効にできるようになりました。
-   サンドボックスワークスペースのハイドレーションでは、ローカル、Docker、プロバイダー支援型のすべてのサンドボックス実装において、絶対パスのシンボリックリンク先を含め、アーカイブルート外を指すシンボリックリンクを含む tar アーカイブが拒否されるようになりました。

### 0.15.0

このバージョンでは、モデルによる拒否が空のテキスト出力として扱われたり、structured outputs の場合に `MaxTurnsExceeded` になるまで実行ループが再試行されたりする代わりに、`ModelRefusalError` として明示的に公開されるようになりました。

これは、拒否のみを含むモデル応答が `final_output == ""` で完了することを想定していたコードに影響します。例外を送出せずに拒否を処理するには、`model_refusal` 実行エラーハンドラーを指定してください。

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs を使用するエージェントでは、ハンドラーからエージェントの出力スキーマに一致する値を返すことができ、SDK は他の実行エラーハンドラーの最終出力と同様にその値を検証します。

### 0.14.0

このマイナーリリースには、破壊的変更は **ありません**。ただし、主要な新しいベータ機能領域である Sandbox エージェントと、ローカル環境、コンテナ環境、ホスト環境で使用するために必要なランタイム、バックエンド、ドキュメントのサポートが追加されています。

主な変更点：

-   `SandboxAgent`、`Manifest`、`SandboxRunConfig` を中心とする新しいベータ版サンドボックスランタイムインターフェースを追加しました。これにより、エージェントは、ファイル、ディレクトリ、Git リポジトリ、マウント、スナップショット、再開機能を備えた永続的で隔離されたワークスペース内で作業できます。
-   `UnixLocalSandboxClient` と `DockerSandboxClient` により、ローカル開発およびコンテナ開発向けのサンドボックス実行バックエンドを追加しました。また、オプションの追加パッケージを通じて、Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel のホスト型プロバイダー統合も追加しました。
-   サンドボックスのメモリサポートを追加し、以降の実行で以前の実行から得た知見を再利用できるようになりました。段階的な情報開示、複数ターンのグループ化、設定可能な分離境界、および S3 支援型ワークフローを含む永続化メモリのコード例が用意されています。
-   ローカルおよび合成ワークスペースエントリ、S3/R2/GCS/Azure Blob Storage/S3 Files のリモートストレージマウント、移植可能なスナップショット、`RunState`、`SandboxSessionState`、または保存済みスナップショットを使用した再開フローを含む、より包括的なワークスペースおよび再開モデルを追加しました。
-   `examples/sandbox/` 以下に、サンドボックスに関する多数のコード例とチュートリアルを追加しました。スキルを使用したコーディングタスク、ハンドオフ、メモリ、プロバイダー固有のセットアップに加え、コードレビュー、データルーム QA、Web サイトのクローン作成などのエンドツーエンドのワークフローを扱っています。
-   サンドボックス対応のセッション準備、機能のバインド、状態のシリアライズ、統合トレーシング、プロンプトキャッシュキーのデフォルト設定、機密性の高い MCP 出力をより安全に秘匿する機能により、コアランタイムとトレーシングスタックを拡張しました。

### 0.13.0

このマイナーリリースには、破壊的変更は **ありません**。ただし、注目すべき Realtime のデフォルト更新、新しい MCP 機能、ランタイムの安定性向上が含まれています。

主な変更点：

-   デフォルトの WebSocket Realtime モデルが `gpt-realtime-1.5` になり、新しい Realtime エージェントのセットアップでは追加の設定なしで新しいモデルが使用されるようになりました。
-   `MCPServer` で `list_resources()`、`list_resource_templates()`、`read_resource()` が公開されるようになりました。また、`MCPServerStreamableHttp` で `session_id` が公開されるようになり、再接続後やステートレスワーカー間でストリーミング可能な HTTP セッションを再開できるようになりました。
-   Chat Completions 統合で、`should_replay_reasoning_content` を通じて推論コンテンツのリプレイをオプトインできるようになりました。これにより、LiteLLM/DeepSeek などのアダプターにおいて、プロバイダー固有の推論やツール呼び出しの継続性が向上します。
-   `SQLAlchemySession` での最初の書き込みの競合、推論の除去後に孤立したアシスタントメッセージ ID を含む圧縮リクエスト、`remove_all_tools()` の実行後も MCP／推論項目が残る問題、関数ツールのバッチ実行機構における競合状態など、ランタイムとセッションに関する複数のエッジケースを修正しました。

### 0.12.0

このマイナーリリースには、破壊的変更は **ありません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)を確認してください。

### 0.11.0

このマイナーリリースには、破壊的変更は **ありません**。主要な機能追加については、[リリースノート](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)を確認してください。

### 0.10.0

このマイナーリリースには、破壊的変更は **ありません**。ただし、OpenAI Responses のユーザー向けに重要な新機能領域である、Responses API の WebSocket トランスポートサポートが含まれています。

主な変更点：

-   OpenAI Responses モデルに WebSocket トランスポートのサポートを追加しました（オプトイン方式であり、HTTP が引き続きデフォルトのトランスポートです）。
-   複数ターンの実行間で、共有の WebSocket 対応プロバイダーと `RunConfig` を再利用するための `responses_websocket_session()` ヘルパー／`ResponsesWebSocketSession` を追加しました。
-   ストリーミング、ツール、承認、フォローアップターンを扱う、新しい WebSocket ストリーミングのコード例（`examples/basic/stream_ws.py`）を追加しました。

### 0.9.0

このバージョンでは、Python 3.9 のサポートを終了しました。このメジャーバージョンは 3 か月前に EOL を迎えています。より新しいランタイムバージョンにアップグレードしてください。

さらに、`Agent#as_tool()` メソッドから返される値の型ヒントが、`Tool` から `FunctionTool` に絞り込まれました。通常、この変更によって破壊的な問題が生じることはありませんが、コードがより広範なユニオン型に依存している場合は、調整が必要になる可能性があります。

### 0.8.0

このバージョンでは、ランタイム動作に関する次の 2 つの変更により、移行作業が必要になる場合があります。

-   **同期** Python 呼び出し可能オブジェクトをラップする関数ツールは、イベントループのスレッド上で実行される代わりに、`asyncio.to_thread(...)` を介してワーカースレッド上で実行されるようになりました。ツールのロジックがスレッドローカル状態やスレッドアフィニティを持つリソースに依存している場合は、非同期ツール実装へ移行するか、ツールのコード内でスレッドアフィニティを明示してください。
-   ローカル MCP ツールの失敗処理が設定可能になり、デフォルトの動作では、実行全体を失敗させる代わりに、モデルから参照可能なエラー出力を返す場合があります。即時失敗の動作に依存している場合は、`mcp_config={"failure_error_function": None}` を設定してください。サーバーレベルの `failure_error_function` の値はエージェントレベルの設定を上書きするため、明示的なハンドラーを持つ各ローカル MCP サーバーで `failure_error_function=None` を設定してください。

### 0.7.0

このバージョンでは、既存のアプリケーションに影響する可能性がある動作変更がいくつかあります。

-   ネストされたハンドオフ履歴は **オプトイン** になりました（デフォルトでは無効です）。v0.6.x のデフォルトのネスト動作に依存していた場合は、`RunConfig(nest_handoff_history=True)` を明示的に設定してください。
-   `gpt-5.1`／`gpt-5.2` のデフォルトの `reasoning.effort` が、SDK のデフォルト設定で指定されていた従来の `"low"` から `"none"` に変更されました。プロンプトまたは品質／コスト特性が `"low"` に依存している場合は、`model_settings` で明示的に設定してください。

### 0.6.0

このバージョンでは、デフォルトのハンドオフ履歴が、生のユーザー／アシスタントのターンを公開する代わりに、1 件のアシスタントメッセージにまとめられるようになりました。これにより、後続のエージェントに簡潔で予測可能な要約が提供されます
-   既存の単一メッセージ形式のハンドオフ記録は、デフォルトで `<CONVERSATION HISTORY>` ブロックの前に "For context, here is the conversation so far between the user and the previous agent:" という文言から始まるようになり、後続のエージェントに明確なラベル付きの要約が提供されます

### 0.5.0

このバージョンでは、目に見える破壊的変更は導入されていませんが、新機能と内部の重要な更新がいくつか含まれています。

-   `RealtimeRunner` に [SIP プロトコル接続](https://platform.openai.com/docs/guides/realtime-sip)を処理するためのサポートを追加しました
-   Python 3.14 との互換性のため、`Runner#run_sync` の内部ロジックを大幅に改訂しました

### 0.4.0

このバージョンでは、[openai](https://pypi.org/project/openai/) パッケージの v1.x バージョンはサポートされなくなりました。この SDK とともに openai v2.x を使用してください。

### 0.3.0

このバージョンでは、Realtime API のサポートが gpt-realtime モデルとその API インターフェース（GA 版）に移行しました。

### 0.2.0

このバージョンでは、以前は引数として `Agent` を受け取っていた箇所の一部が、代わりに `AgentBase` を受け取るようになりました。たとえば、MCP サーバーの `list_tools()` 呼び出しが該当します。これは純粋に型付け上の変更であり、引き続き `Agent` オブジェクトを受け取ります。更新するには、`Agent` を `AgentBase` に置き換えて型エラーを修正するだけです。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に `run_context` と `agent` という 2 つの新しいパラメーターが追加されました。`MCPServer` を継承するすべてのクラスに、これらのパラメーターを追加する必要があります。