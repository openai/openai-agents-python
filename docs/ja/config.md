---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK はインポートされるとすぐに LLM リクエストとトレーシングのために `OPENAI_API_KEY` 環境変数を探します。アプリ起動前にその環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、 SDK は環境変数で指定された API キーまたは上記で設定したデフォルトキーを使って `AsyncOpenAI` インスタンスを生成します。 [set_default_openai_client()][agents.set_default_openai_client] 関数を使うことでこれを変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることも可能です。デフォルトでは OpenAI Responses API を使用しますが、 [set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API に変更できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは、上記セクションと同じ OpenAI API キー（環境変数または設定したデフォルトキー）を使用します。 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使ってトレーシングに使用する API キーを個別に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用してトレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグロギング

 SDK にはハンドラーが設定されていない Python ロガーが 2 つあります。デフォルトでは警告とエラーが `stdout` に出力され、その他のログは抑制されます。

詳細なログを有効にするには、 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることもできます。詳細は [Python logging ガイド](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

一部のログには機密データ（例: ユーザー データ）が含まれる場合があります。これらのデータをログに残さないようにするには、次の環境変数を設定してください。

LLM の入力と出力をログに残さないようにするには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力をログに残さないようにするには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```