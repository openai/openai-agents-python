---
search:
  exclude: true
---
# 設定

このページでは、デフォルトの OpenAI キーまたはクライアント、デフォルトの OpenAI API の形式、トレーシングエクスポートのデフォルト、ログ動作など、通常はアプリケーションの起動時に一度だけ設定する SDK 全体のデフォルトについて説明します。

これらのデフォルトはサンドボックスベースのワークフローにも引き続き適用されますが、サンドボックスワークスペース、サンドボックスクライアント、セッションの再利用は別途設定します。

代わりに特定のエージェントまたは実行を設定する必要がある場合は、まず次を参照してください:

-   [エージェント](agents.md): 通常の `Agent` の instructions、tools、出力タイプ、ハンドオフ、ガードレールについて。
-   [エージェントの実行](running_agents.md): `RunConfig`、セッション、会話状態オプションについて。
-   [サンドボックスエージェント](sandbox/guide.md): `SandboxRunConfig`、マニフェスト、ケイパビリティ、サンドボックスクライアント固有のワークスペース設定について。
-   [モデル](models/index.md): モデル選択とプロバイダー設定について。
-   [トレーシング](tracing.md): 実行ごとのトレーシングメタデータとカスタムトレースプロセッサーについて。

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。このキーは、SDK が初めて OpenAI クライアントを作成するときに解決されます（遅延初期化）。そのため、最初のモデル呼び出しの前に環境変数を設定してください。アプリの起動前にその環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数の API キー、または上で設定したデフォルトキーを使用して、`AsyncOpenAI` インスタンスを作成します。これは [set_default_openai_client()][agents.set_default_openai_client] 関数を使用して変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

環境変数ベースのエンドポイント設定を使用したい場合、デフォルトの OpenAI プロバイダーは `OPENAI_BASE_URL` も読み取ります。Responses WebSocket トランスポートを有効にすると、WebSocket の `/responses` エンドポイント用に `OPENAI_WEBSOCKET_BASE_URL` も読み取ります。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最後に、使用される OpenAI API もカスタマイズできます。デフォルトでは OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使用すると、これを上書きして Chat Completions API を使用できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI プロバイダーのデフォルト

OpenAI を基盤とするプロバイダーは、モデル名を解決するときにも SDK 全体のデフォルトを読み取ります。OpenAI Responses モデルがデフォルトで WebSocket トランスポートを使用するようにするには、[`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport] を使用します:

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデルに影響します。プロバイダーレベルのセットアップ、接続の再利用、キープアライブオプション、カスタム WebSocket エンドポイントについては、[Responses WebSocket トランスポート](models/index.md#responses-websocket-transport) を参照してください。

OpenAI セットアップでプロバイダーレベルのエージェント登録メタデータが必要な場合は、起動時にデフォルトのハーネス ID を一度設定してください:

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

完全な登録オブジェクトを渡すこともできます:

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

SDK のデフォルトが設定されていない場合、OpenAI を基盤とするプロバイダーは `OPENAI_AGENT_HARNESS_ID` 環境変数にフォールバックします。ハーネス ID が設定されている場合、`agent_harness_id` が `RunConfig.trace_metadata` にすでに存在する場合を除き、SDK はそれを `agent_harness_id` としてトレースメタデータに追加します。

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上のセクションで説明したモデルリクエストと同じ OpenAI API キー（つまり、環境変数または設定したデフォルトキー）を使用します。トレーシングに使用する API キーは、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用して個別に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

モデルのトラフィックではあるキーまたはクライアントを使用し、トレーシングでは別の OpenAI キーを使用したい場合は、デフォルトのキーまたはクライアントを設定するときに `use_for_tracing=False` を渡し、そのうえでトレーシングを個別に設定してください。カスタムクライアントを使用していない場合は、同じパターンを [`set_default_openai_key()`][agents.set_default_openai_key] にも適用できます。

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key")
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

デフォルトエクスポーターを使用するときに、トレースを特定の組織またはプロジェクトに関連付ける必要がある場合は、アプリの起動前に次の環境変数を設定してください:

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

グローバルエクスポーターを変更せずに、実行ごとにトレーシング API キーを設定することもできます。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用して、トレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

トレーシングを有効のままにしつつ、機微情報を含む可能性のある入力/出力をトレースペイロードから除外したい場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定してください:

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

コードを使わずにデフォルトを変更することもできます。アプリの起動前にこの環境変数を設定してください:

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシング制御の詳細については、[トレーシングガイド](tracing.md) を参照してください。

## デバッグログ

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しており、デフォルトではハンドラーをアタッチしません。ログは、アプリケーションの Python ロギング設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることもできます。詳しくは [Python ロギングガイド](https://docs.python.org/3/howto/logging.html) を参照してください。

```python
import logging

logger = logging.getLogger("openai.agents") # or openai.agents.tracing for the Tracing logger

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

### ログ内の機微データ

一部のログには、機微データ（たとえば、ユーザーデータ）が含まれる場合があります。

デフォルトでは、SDK は LLM の入力/出力やツールの入力/出力を **ログに記録しません** 。これらの保護は次の項目で制御されます:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのためにこのデータを一時的に含める必要がある場合は、アプリの起動前にいずれかの変数を `0`（または `false`）に設定してください:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```