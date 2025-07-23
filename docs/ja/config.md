---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK は import された時点で、 LLM リクエストとトレーシング用に `OPENAI_API_KEY` 環境変数を探します。アプリを起動する前にその環境変数を設定できない場合は、[`set_default_openai_key()`][agents.set_default_openai_key] 関数を使ってキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

別の方法として、使用する OpenAI クライアントを設定することもできます。デフォルトでは、 SDK は環境変数または上記で設定したデフォルトキーを用いて `AsyncOpenAI` インスタンスを生成します。これを変更したい場合は、[`set_default_openai_client()`][agents.set_default_openai_client] 関数を使用してください。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることも可能です。デフォルトでは、 OpenAI Responses API を使用します。これを Chat Completions API に変更したい場合は、[`set_default_openai_api()`][agents.set_default_openai_api] 関数をご利用ください。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは、上記のセクションで設定した OpenAI API キー（環境変数またはデフォルトキー）を使用します。トレーシングで使用する API キーを個別に設定したい場合は、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

さらに、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使うことで、トレーシングを完全に無効化できます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

 SDK には、ハンドラーが設定されていない Python ロガーが 2 つあります。デフォルトでは、警告とエラーのみが `stdout` に出力され、それ以外のログは抑制されます。

詳細なログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることもできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

### ログに含まれる機微なデータ

一部のログには機微なデータ（例: ユーザー データ）が含まれる場合があります。これらのデータの記録を無効化したい場合は、以下の環境変数を設定してください。

LLM の入力および出力のロギングを無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力のロギングを無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```