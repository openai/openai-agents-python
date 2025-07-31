---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、SDK はインポート時に LLM リクエストおよびトレーシング用の `OPENAI_API_KEY` 環境変数を探します。アプリ起動前にこの環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

別の方法として、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数で指定された API キー、または前述の既定キーを用いて `AsyncOpenAI` インスタンスを生成します。これを変更するには、[set_default_openai_client()][agents.set_default_openai_client] 関数を利用してください。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、利用する OpenAI API をカスタマイズすることも可能です。デフォルトでは OpenAI Responses API が使用されます。これを Chat Completions API に切り替えるには、[set_default_openai_api()][agents.set_default_openai_api] 関数を使用してください。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。上記セクションの OpenAI API キー (環境変数または設定済みの既定キー) が自動的に使用されます。トレーシング専用の API キーを設定するには、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効化したい場合は、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を呼び出してください。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグ ロギング

SDK にはハンドラーが設定されていない Python ロガーが 2 つ用意されています。デフォルトでは、warning と error は `stdout` に出力されますが、それ以外のログは抑制されています。

詳細なログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を呼び出してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー・フィルター・フォーマッターなどを追加してログをカスタマイズすることもできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) を参照してください。

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

一部のログには (たとえば ユーザー データ など) 機密データが含まれる場合があります。これらを記録したくない場合は、次の環境変数を設定してください。

LLM の入力と出力のロギングを無効にする:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力のロギングを無効にする:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```