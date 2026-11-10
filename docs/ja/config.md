---
search:
  exclude: true
---
# 設定

このページでは、アプリケーションの起動時に通常一度だけ設定する SDK 全体のデフォルトについて説明します。たとえば、デフォルトの OpenAI キーまたはクライアント、デフォルトの OpenAI API 形式、トレーシングのエクスポート設定のデフォルト、ログ動作などです。

これらのデフォルトはサンドボックスベースのワークフローにも適用されますが、サンドボックスワークスペース、サンドボックスクライアント、セッションの再利用は個別に設定します。

代わりに特定のエージェントまたは実行を設定する必要がある場合は、以下から始めてください。

-   通常の `Agent` における instructions、tools、出力型、ハンドオフ、ガードレールについては、[エージェント](agents.md)を参照してください。
-   `RunConfig`、セッション、会話状態のオプションについては、[エージェントの実行](running_agents.md)を参照してください。
-   `SandboxRunConfig`、マニフェスト、ケイパビリティ、サンドボックスクライアント固有のワークスペース設定については、[サンドボックスエージェント](sandbox/guide.md)を参照してください。
-   モデルの選択とプロバイダー設定については、[モデル](models/index.md)を参照してください。
-   実行ごとのトレーシングメタデータとカスタムトレースプロセッサーについては、[トレーシング](tracing.md)を参照してください。

## 設定オブジェクトと辞書 {#configuration-objects-and-dictionaries}

SDK で定義されている設定パラメーターは、通常、型付きの設定オブジェクト、または同じフィールドを含む辞書のいずれかを受け取ります。これは、型アノテーションに辞書が含まれる、エージェント、実行、モデル、セッション、サンドボックス、音声の各設定境界に適用されます。SDK で定義されているネストされた設定型にも辞書を使用できます。

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings={
        "reasoning": {"effort": "high"},
        "verbosity": "low",
    },
)
```

SDK は、これらの辞書を対応する設定オブジェクトへ正規化します。SDK で定義されたデータクラス設定型に未知のフィールドがある場合は `TypeError` が発生するため、オプション名のスペルミスを早期に検出できます。特定の境界が辞書を受け取るかどうかは、パラメーターの型アノテーションまたは API リファレンスで確認してください。

## API キーとクライアント {#api-keys-and-clients}

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。キーは、SDK が初めて OpenAI クライアントを作成するときに解決されるため（遅延初期化）、最初のモデル呼び出しより前に環境変数を設定してください。アプリケーションの起動前にその環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数の API キーまたは上記で設定したデフォルトキーを使用して、`AsyncOpenAI` インスタンスを作成します。[set_default_openai_client()][agents.set_default_openai_client] 関数を使用すると、この動作を変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

明示的なクライアントを [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider] に渡す場合、そのクライアントが接続設定とアカウント設定を管理します。`api_key`、`base_url`、`websocket_base_url`、`organization`、`project` を `OpenAIProvider` に同時に渡さないでください。`openai_client` をこれらの引数のいずれかと組み合わせると、重複した値が暗黙的に無視されるのではなく、[`UserError`][agents.exceptions.UserError] が発生します。意図する値は `AsyncOpenAI` の構築時に設定してください。

`openai_client` を省略した場合、`OpenAIProvider` が SDK 全体のデフォルトクライアントを再利用するのは、`api_key`、`base_url`、`websocket_base_url`、`organization`、`project` がすべて `None` の場合に限られます。空文字列を含め、これらのオプションのいずれかを渡すと、プロバイダーは独自のクライアントを作成し、そのプロバイダーオプションが SDK 全体のデフォルトクライアントより優先されます。プロバイダーが `set_default_openai_client()` で設定されたクライアントを継承する必要がある場合は、すべてのプロバイダーオプションを `None` のままにしてください。

[`OpenAIVoiceModelProvider`][agents.voice.models.openai_model_provider.OpenAIVoiceModelProvider] では、`api_key`、`base_url`、`organization`、`project` に同じ所有権と優先順位の規則が適用されます。明示的な `openai_client` をこれら 4 つのオプションのいずれかと組み合わせることはできません。

### `openai` v3 対応のカスタム HTTP クライアント {#custom-http-clients-with-openai-v3}

バージョン 0.21.0 では `openai>=3.0.0,<4` が必要です。デフォルトの OpenAI プロバイダーは HTTPX2 を使用するため、ほとんどのアプリケーションでは HTTP クライアントを直接設定する必要はありません。アプリケーションが `http_client=` を `AsyncOpenAI` に渡す場合は、カスタムクライアントとそのトランスポート関連オプションに HTTPX2 の型を使用してください。

```python
import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from agents import set_default_openai_client

http_client = DefaultAsyncHttpx2Client(
    timeout=httpx2.Timeout(30.0, connect=5.0),
)
custom_client = AsyncOpenAI(
    api_key="...",
    http_client=http_client,
)
set_default_openai_client(custom_client)
```

同じ移行は、カスタムトランスポート、認証、イベントフック、モックトランスポート、URL、リクエスト、レスポンス、トランスポート例外の処理にも適用されます。それぞれに対応する `httpx2` を使用してください。Agents SDK は、任意の旧式の `httpx` オブジェクトを HTTPX2 に変換しません。アプリケーションが `httpx` を明示的にインストールした場合、OpenAI Python SDK は旧式クライアント向けの一時的な互換手段を提供しますが、新規コードおよび移行後のコードでは HTTPX2 を使用してください。

この OpenAI クライアントの境界は、ローカル MCP トランスポートのカスタマイズとは別です。MCP Python SDK v1 は独自の旧式 `httpx` 依存関係を使用し、MCP Python SDK v2 は `httpx2` を使用します。詳細は [MCP Python SDK v1 と v2](mcp.md#mcp-python-sdk-v1-and-v2)を参照してください。

環境変数によるエンドポイント設定を使用する場合、デフォルトの OpenAI プロバイダーは `OPENAI_BASE_URL` も読み取ります。Responses の WebSocket トランスポートを有効にすると、WebSocket の `/responses` エンドポイント用に `OPENAI_WEBSOCKET_BASE_URL` も読み取ります。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最後に、使用する OpenAI API もカスタマイズできます。デフォルトでは OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使用すると、これをオーバーライドして Chat Completions API を使用できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI プロバイダーのデフォルト {#openai-provider-defaults}

SDK の OpenAI バックエンドを使用するプロバイダーは、モデル名の文字列をモデルにマッピングするときに、SDK 全体のデフォルトも読み取ります。OpenAI Responses モデルでデフォルトとして WebSocket トランスポートを使用するには、[`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport] を使用します。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーがモデル名を解決した結果として得られる OpenAI Responses モデルに影響します。プロバイダーレベルの設定、接続の再利用、キープアライブオプション、カスタム WebSocket エンドポイントについては、[Responses WebSocket トランスポート](models/index.md#responses-websocket-transport)を参照してください。

OpenAI の設定でプロバイダーレベルのエージェント登録メタデータが必要な場合は、起動時にデフォルトのハーネス ID を一度設定します。

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

完全な登録オブジェクトを渡すこともできます。

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(OpenAIAgentRegistrationConfig(harness_id="your-harness-id"))
```

SDK のデフォルトが設定されていない場合、SDK の OpenAI バックエンドを使用するプロバイダーは `OPENAI_AGENT_HARNESS_ID` 環境変数にフォールバックします。ハーネス ID が設定されている場合、`RunConfig.trace_metadata` にそのキーがすでに存在しない限り、SDK はそれをトレースメタデータへ `agent_harness_id` として追加します。

## トレーシング {#tracing}

トレーシングはデフォルトで有効です。デフォルトでは、上記のセクションでモデルリクエストに使用するものと同じ OpenAI API キー、つまり環境変数または設定したデフォルトキーを使用します。トレーシングに使用する API キーは、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用して個別に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

モデルの通信にあるキーまたはクライアントを使用し、トレーシングには別の OpenAI キーを使用する場合は、デフォルトのキーまたはクライアントを設定するときに `use_for_tracing=False` を渡してから、トレーシングを個別に設定します。カスタムクライアントを使用していない場合は、[`set_default_openai_key()`][agents.set_default_openai_key] でも同じ方法を使用できます。

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(
    base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key"
)
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

デフォルトのエクスポーターを使用して、トレースを特定の組織またはプロジェクトに関連付ける必要がある場合は、アプリケーションの起動前に以下の環境変数を設定してください。

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

グローバルエクスポーターを変更せずに、実行ごとのトレーシング API キーを設定することもできます。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用すると、トレーシングを完全に無効にすることもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

トレーシングを有効にしたまま、機密情報が含まれる可能性のある入力や出力をトレースペイロードから除外するには、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定します。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

アプリケーションの起動前に以下の環境変数を設定すると、コードを変更せずにデフォルトを変更することもできます。

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシングのすべての制御については、[トレーシングガイド](tracing.md)を参照してください。

## デバッグログ {#debug-logging}

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しますが、デフォルトではハンドラーを追加しません。ログは、アプリケーションの Python ロギング設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、ハンドラー、フィルター、フォーマッターなどを追加して、ログをカスタマイズできます。詳細は [Python ロギングガイド](https://docs.python.org/3/howto/logging.html)を参照してください。

```python
import logging

logger = logging.getLogger("openai.agents")  # or openai.agents.tracing for the Tracing logger

# To make all logs show up
logger.setLevel(logging.DEBUG)
# To make info and above show up
logger.setLevel(logging.INFO)
# To make warning and above show up
logger.setLevel(logging.WARNING)
# etc

# You can customize this as needed, but this will output to `stderr` by default
logger.addHandler(logging.StreamHandler())
```

### ログと診断に含まれる機密データ {#sensitive-data-in-logs-and-diagnostics}

一部のログや診断例外には、機密データが含まれる場合があります。たとえば、モデルやツールの入力と出力などです。

デフォルトでは、SDK は LLM の入力/出力やツールの入力/出力を **ログに記録しません**。これらの保護は、以下によって制御されます。

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのためにこのデータを一時的に含める必要がある場合は、アプリケーションの起動前に、いずれかの変数を `0`（または `false`）に設定します。

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```

これらのフラグは、影響を受ける失敗で、ペイロードを含む診断詳細を保持するかどうかも制御します。たとえば、ツールデータの秘匿化が有効な場合、`FunctionTool` に無効な引数を渡すと、基となる検証エラーを例外チェーンに含めず、汎用的な `ModelBehaviorError` が発生します。いずれかの変数を `0` に設定すると、モデルまたはツールの生データが、ログ、例外メッセージ、例外チェーン、その他の診断コンテキストに公開される可能性があります。そのため、管理された開発環境でのみ有効にしてください。