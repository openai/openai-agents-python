---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、SDK をインポートした直後から、LLM リクエストとトレーシングに使用する環境変数 `OPENAI_API_KEY` を参照します。アプリ起動前にこの環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数でキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを個別に設定することも可能です。デフォルトでは、SDK は環境変数または上記で設定したデフォルトキーを用いて `AsyncOpenAI` インスタンスを生成します。[set_default_openai_client()][agents.set_default_openai_client] 関数を使うことで、この設定を変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API もカスタマイズできます。デフォルトでは OpenAI Responses API を利用しますが、[set_default_openai_api()][agents.set_default_openai_api] 関数を使用して Chat Completions API に切り替えられます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。上記セクションで説明した OpenAI API キー（環境変数または設定したデフォルトキー）が使用されます。トレーシング専用の API キーを指定したい場合は、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を利用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効にする場合は、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用します。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグ ロギング

SDK にはハンドラーが設定されていない Python ロガーが 2 つあります。デフォルトでは、警告とエラーは `stdout` に送られますが、それ以外のログは抑制されます。

詳細なロギングを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

ログを独自にカスタマイズしたい場合は、ハンドラー、フィルター、フォーマッターなどを追加できます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

一部のログには機密データ（例：ユーザーデータ）が含まれる場合があります。こうしたデータの出力を無効にしたい場合は、以下の環境変数を設定してください。

LLM の入力と出力のロギングを無効にする:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力のロギングを無効にする:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```