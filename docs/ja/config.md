---
search:
  exclude: true
---
# 設定

このページでは、通常はアプリケーション起動時に 1 度だけ設定する SDK 全体のデフォルト（デフォルトの OpenAI キーまたはクライアント、デフォルトの OpenAI API 形式、トレーシングエクスポートのデフォルト、ログ動作など）を扱います。

これらのデフォルトは sandbox ベースのワークフローにも適用されますが、sandbox ワークスペース、sandbox クライアント、セッション再利用は別途設定します。

代わりに特定のエージェントや実行を設定する必要がある場合は、次から始めてください:

-   通常の `Agent` における instructions、ツール、出力タイプ、ハンドオフ、ガードレールについては [Agents](agents.md)。
-   `RunConfig`、セッション、会話状態オプションについては [エージェントの実行](running_agents.md)。
-   `SandboxRunConfig`、マニフェスト、機能、sandbox クライアント固有のワークスペース設定については [Sandbox エージェント](sandbox/guide.md)。
-   モデル選択とプロバイダー設定については [Models](models/index.md)。
-   実行ごとのトレーシングメタデータとカスタムトレースプロセッサーについては [トレーシング](tracing.md)。

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。キーは SDK が最初に OpenAI クライアントを作成する際（遅延初期化）に解決されるため、最初のモデル呼び出し前に環境変数を設定してください。アプリ起動前にその環境変数を設定できない場合は、キーを設定するために [set_default_openai_key()][agents.set_default_openai_key] 関数を使用できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

または、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数の API キーまたは上記で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを作成します。これは [set_default_openai_client()][agents.set_default_openai_client] 関数で変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

環境変数ベースのエンドポイント設定を使いたい場合、デフォルトの OpenAI プロバイダーは `OPENAI_BASE_URL` も読み取ります。Responses websocket トランスポートを有効にすると、websocket `/responses` エンドポイント用に `OPENAI_WEBSOCKET_BASE_URL` も読み取ります。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用します。これは [set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API を使うように上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上のセクションのモデルリクエストと同じ OpenAI API キー（つまり環境変数または設定したデフォルトキー）を使用します。トレーシングに使用する API キーは [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数で明示的に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

モデル通信があるキーまたはクライアントを使い、トレーシングは別の OpenAI キーを使う必要がある場合、デフォルトキーまたはクライアント設定時に `use_for_tracing=False` を渡してから、トレーシングを個別に設定してください。カスタムクライアントを使わない場合は [`set_default_openai_key()`][agents.set_default_openai_key] でも同じパターンが使えます。

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

デフォルトのエクスポーター使用時に、トレースを特定の組織またはプロジェクトに紐付ける必要がある場合は、アプリ起動前に以下の環境変数を設定してください:

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

トレーシングを有効のまま、トレースペイロードから機密性の高い可能性がある入出力を除外したい場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定してください:

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

アプリ起動前にこの環境変数を設定すれば、コードなしでデフォルトを変更することもできます:

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシング制御の全体については、[トレーシングガイド](tracing.md) を参照してください。

## デバッグログ

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しており、デフォルトではハンドラーをアタッチしません。ログはアプリケーションの Python ログ設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

### ログ内の機密データ

特定のログには機密データ（たとえばユーザーデータ）が含まれる場合があります。

デフォルトでは、SDK は LLM の入出力やツールの入出力を **ログに記録しません**。これらの保護は次によって制御されます:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのために一時的にこれらのデータを含める必要がある場合は、アプリ起動前にいずれかの変数を `0`（または `false`）に設定してください:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```