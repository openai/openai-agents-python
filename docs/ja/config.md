---
search:
  exclude: true
---
# 設定

このページでは、通常はアプリケーション起動時に一度だけ設定する SDK 全体のデフォルト値（デフォルトの OpenAI キーまたはクライアント、デフォルトの OpenAI API 形式、トレーシングエクスポートのデフォルト値、ロギング動作など）について説明します。

これらのデフォルト値は sandbox ベースのワークフローにも適用されますが、sandbox ワークスペース、sandbox クライアント、セッション再利用は個別に設定します。

代わりに特定のエージェントまたは実行を設定する必要がある場合は、次から始めてください。

-   通常の `Agent` における instructions、tools、出力型、ハンドオフ、ガードレールは [Agents](agents.md) を参照してください。
-   `RunConfig`、セッション、会話状態オプションは [Running agents](running_agents.md) を参照してください。
-   `SandboxRunConfig`、マニフェスト、機能、sandbox クライアント固有のワークスペース設定は [Sandbox agents](sandbox/guide.md) を参照してください。
-   モデル選択とプロバイダー設定は [Models](models/index.md) を参照してください。
-   実行ごとのトレーシングメタデータとカスタムトレースプロセッサーは [Tracing](tracing.md) を参照してください。

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。キーは SDK が最初に OpenAI クライアントを作成するとき（遅延初期化）に解決されるため、最初のモデル呼び出し前に環境変数を設定してください。アプリ起動前にその環境変数を設定できない場合は、キーを設定するために [set_default_openai_key()][agents.set_default_openai_key] 関数を使用できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数の API キーまたは上記で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを作成します。これは [set_default_openai_client()][agents.set_default_openai_client] 関数で変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用します。これは [set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API を使用するように上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上記セクションのモデルリクエストと同じ OpenAI API キー（つまり、環境変数または設定したデフォルトキー）を使用します。トレーシングに使用する API キーを明示的に設定するには、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用します。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

デフォルトエクスポーター使用時に、特定の組織またはプロジェクトにトレースを紐付ける必要がある場合は、アプリ起動前に次の環境変数を設定してください。

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

トレーシングは有効のままにしつつ、機密性がある可能性のある入力/出力をトレースペイロードから除外したい場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定します。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

コードを書かずにデフォルトを変更するには、アプリ起動前にこの環境変数を設定することもできます。

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシング制御の全体については、[tracing guide](tracing.md) を参照してください。

## デバッグロギング

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しており、デフォルトではハンドラーをアタッチしません。ログはアプリケーションの Python ロギング設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることもできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

一部のログには機密データ（たとえば、ユーザーデータ）が含まれる場合があります。

デフォルトでは、SDK は LLM の入力/出力やツールの入力/出力を **記録しません**。これらの保護は次で制御されます。

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのために一時的にこのデータを含める必要がある場合は、アプリ起動前にいずれかの変数を `0`（または `false`）に設定してください。

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```