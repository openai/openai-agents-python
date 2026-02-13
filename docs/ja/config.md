---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK はインポートされるとすぐに、 LLM リクエストとトレーシングのために `OPENAI_API_KEY` 環境変数を探します。 アプリの起動前にその環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、 使用する OpenAI クライアントを設定することもできます。 デフォルトでは、 SDK は環境変数の API キー、 または上で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを作成します。 これは、 [set_default_openai_client()][agents.set_default_openai_client] 関数を使用して変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、 使用する OpenAI API もカスタマイズできます。 デフォルトでは、 OpenAI Responses API を使用します。 [set_default_openai_api()][agents.set_default_openai_api] 関数を使用して、 これを上書きし Chat Completions API を使用できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。 デフォルトでは上記セクションの OpenAI API キー（つまり、 環境変数または設定したデフォルトキー）を使用します。 トレーシングに使用する API キーを明示的に設定するには、 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用します。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

デフォルトの exporter を使用する際に、 トレースを特定の組織またはプロジェクトに紐付ける必要がある場合は、 アプリの起動前に次の環境変数を設定してください。

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

グローバル exporter を変更せずに、 実行ごとにトレーシング API キーを設定することもできます。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用して、 トレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しており、 デフォルトではハンドラーをアタッチしません。 ログは、 アプリケーションの Python logging 設定に従います。

詳細なログを有効にするには、 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

または、 ハンドラー、 フィルター、 フォーマッターなどを追加してログをカスタマイズできます。 詳細は、 [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

### ログ内の機微情報

一部のログには機微情報（例: ユーザーデータ）が含まれる場合があります。 こうしたデータがログに記録されないようにするには、 次の環境変数を設定してください。

LLM の入力と出力のログ記録を無効化するには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力のログ記録を無効化するには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```