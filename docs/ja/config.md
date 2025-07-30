---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK はインポートされた直後に LLM へのリクエストとトレーシングのための `OPENAI_API_KEY` 環境変数を検索します。アプリ起動前にこの環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数でキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

代わりに、使用する OpenAI クライアントを設定することもできます。デフォルトでは、 SDK は環境変数または上記で設定したデフォルトキーを使って `AsyncOpenAI` インスタンスを生成します。この動作は [set_default_openai_client()][agents.set_default_openai_client] 関数で変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることも可能です。デフォルトでは OpenAI Responses API を使用していますが、 [set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API に変更できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上記セクションで説明した OpenAI API キー（環境変数または設定したデフォルトキー）が使用されます。トレーシングで使用する API キーを個別に設定したい場合は、 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効化するには、 [`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使います。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグロギング

SDK にはハンドラーが設定されていない Python ロガーが 2 つあります。デフォルトでは、警告とエラーが `stdout` に送られ、その他のログは抑制されます。

詳細なログを有効にするには、 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることもできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

### ログに含まれる機密データ

一部のログには機密データ（たとえば ユーザー データ）が含まれる場合があります。これらのデータが記録されないようにするには、次の環境変数を設定してください。

LLM の入力と出力のログを無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力のログを無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```