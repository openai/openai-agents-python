---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

既定では、 SDK はインポート時に LLM リクエストおよびトレーシング用の `OPENAI_API_KEY` 環境変数を参照します。アプリ起動前にこの環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

別の方法として、使用する OpenAI クライアントを構成することもできます。既定では、 SDK は `AsyncOpenAI` インスタンスを作成し、前述の環境変数またはデフォルトキーを使用します。 [set_default_openai_client()][agents.set_default_openai_client] 関数を使ってこれを変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API をカスタマイズすることも可能です。既定では OpenAI Responses API が使用されます。 [set_default_openai_api()][agents.set_default_openai_api] 関数を使用することで、 Chat Completions API へ切り替えられます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングは既定で有効になっています。上記で説明した OpenAI API キー (環境変数または設定したデフォルトキー) が使用されます。トレーシングに使用する API キーを個別に設定したい場合は、 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を利用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効化したい場合は、 [`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用します。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

SDK にはハンドラーが設定されていない Python ロガーが 2 つあります。既定では、警告とエラーは `stdout` に出力されますが、それ以外のログは抑制されます。

詳細なログを有効にするには、 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることも可能です。詳細は [Python logging ガイド](https://docs.python.org/3/howto/logging.html) を参照してください。

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

### ログ内の機微データ

一部のログには (ユーザーデータなどの) 機微データが含まれる場合があります。これらのデータをログに出力したくない場合は、次の環境変数を設定してください。

LLM の入力および出力のログを無効化する:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力のログを無効化する:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```