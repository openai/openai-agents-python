---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、SDK はインポートされるとすぐに LLM リクエストとトレーシングのために `OPENAI_API_KEY` 環境変数を探します。アプリ起動前にこの環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数または上記で設定したデフォルトキーを利用して `AsyncOpenAI` インスタンスを生成します。これを変更したい場合は、[set_default_openai_client()][agents.set_default_openai_client] 関数を使用してください。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API をカスタマイズすることも可能です。標準では OpenAI Responses API を使用しますが、[set_default_openai_api()][agents.set_default_openai_api] 関数を用いて Chat Completions API に切り替えられます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは前述の OpenAI API キー（環境変数または設定したデフォルトキー）を使用します。トレーシング専用の API キーを指定したい場合は、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効化する場合は、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を呼び出します。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグ ログ

SDK にはハンドラーが設定されていない Python ロガーが 2 つあります。デフォルトでは、警告とエラーは `stdout` に送信されますが、それ以外のログは抑制されます。

詳細なログ出力を有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

ログをカスタマイズしたい場合は、ハンドラー・フィルター・フォーマッターなどを追加できます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

### ログの機微データ

一部のログには、ユーザー データなどの機微な情報が含まれる場合があります。これらのデータをログに残したくない場合は、以下の環境変数を設定してください。

LLM の入力および出力を記録しない:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力を記録しない:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```