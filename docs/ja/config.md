---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストと トレーシング に `OPENAI_API_KEY` 環境変数を使用します。このキーは、SDK が最初に OpenAI クライアントを作成するとき（遅延初期化）に解決されるため、最初の モデル 呼び出しの前に環境変数を設定してください。アプリの起動前にその環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使ってキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

代わりに、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は `AsyncOpenAI` インスタンスを作成し、環境変数の API キー、または上で設定したデフォルトキーを使用します。これは [set_default_openai_client()][agents.set_default_openai_client] 関数で変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API もカスタマイズできます。デフォルトでは OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使うことで、Chat Completions API を使用するように上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシング はデフォルトで有効です。デフォルトでは、上のセクションの OpenAI API キー（つまり、環境変数または設定したデフォルトキー）を使用します。トレーシング に使用する API キーを明示的に設定するには、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用します。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

デフォルトのエクスポーターを使用する際に、トレースを特定の organization または project に紐付ける必要がある場合は、アプリの起動前に次の環境変数を設定してください:

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

グローバルなエクスポーターを変更せずに、実行ごとに トレーシング の API キーを設定することもできます。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用すると、トレーシング を完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

トレーシング は有効のままにしつつ、トレースのペイロードから機密性の高い可能性がある入力 / 出力を除外したい場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定します:

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

コードなしでデフォルトを変更するには、アプリの起動前にこの環境変数を設定することもできます:

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシング の制御の全体像については、[トレーシング ガイド](tracing.md) を参照してください。

## デバッグ ロギング

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しており、デフォルトではハンドラーをアタッチしません。ログは、アプリケーションの Python ロギング設定に従います。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズできます。詳しくは [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

一部のログには機密データ（例: ユーザー データ）が含まれる場合があります。

デフォルトでは、SDK は LLM の入力 / 出力やツールの入力 / 出力をログに **記録しません**。これらの保護は次で制御されます:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのために一時的にこのデータを含める必要がある場合は、アプリの起動前にいずれかの変数を `0`（または `false`）に設定してください:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```