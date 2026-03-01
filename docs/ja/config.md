---
search:
  exclude: true
---
# SDK の設定

このページでは、通常アプリケーション起動時に一度だけ設定する SDK 全体のデフォルト値（デフォルトの OpenAI key や client、デフォルトの OpenAI API 形式、トレーシングのエクスポートデフォルト、ロギング動作など）を扱います。

代わりに特定のエージェントや実行を設定する必要がある場合は、まず次をご覧ください。

-   `RunConfig`、セッション、会話状態オプションについては [エージェントの実行](running_agents.md)。
-   モデル選択とプロバイダー設定については [Models](models/index.md)。
-   実行ごとのトレーシングメタデータとカスタムトレースプロセッサーについては [Tracing](tracing.md)。

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。キーは SDK が最初に OpenAI client を作成する際（遅延初期化）に解決されるため、最初のモデル呼び出し前に環境変数を設定してください。アプリ起動前にその環境変数を設定できない場合は、キーを設定するために [set_default_openai_key()][agents.set_default_openai_key] 関数を使用できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI client を設定することもできます。デフォルトでは、SDK は `AsyncOpenAI` インスタンスを作成し、環境変数または上で設定したデフォルトキーの API キーを使用します。これは [set_default_openai_client()][agents.set_default_openai_client] 関数で変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使用して、これを Chat Completions API に上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上記セクションのモデルリクエストと同じ OpenAI API キー（つまり環境変数または設定したデフォルトキー）を使用します。トレーシングで使用する API キーは、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数で明示的に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

デフォルトエクスポーターを使用する際にトレースを特定の organization や project に紐付ける必要がある場合は、アプリ起動前に次の環境変数を設定してください。

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

トレーシングは有効のままにしつつ、トレースペイロードから機密性の高い可能性がある入力/出力を除外したい場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

コードを変更せずにデフォルトを変更するには、アプリ起動前に次の環境変数を設定することもできます。

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシング制御の全体については、[トレーシングガイド](tracing.md) を参照してください。

## デバッグロギング

SDK は 2 つの Python logger（`openai.agents` と `openai.agents.tracing`）を定義し、デフォルトでは handler をアタッチしません。ログはアプリケーションの Python logging 設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、handler、filter、formatter などを追加してログをカスタマイズできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

一部のログには機密データ（例: ユーザーデータ）が含まれる可能性があります。

デフォルトでは、SDK は LLM の入力/出力やツールの入力/出力を **記録しません** 。これらの保護は次で制御されます。

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのために一時的にこのデータを含める必要がある場合は、アプリ起動前にいずれかの変数を `0`（または `false`）に設定してください。

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```