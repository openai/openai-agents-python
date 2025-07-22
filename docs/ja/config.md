---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK はインポートされるとすぐに LLM リクエストとトレーシング用に `OPENAI_API_KEY` 環境変数を探します。アプリ起動前にその環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAIクライアントを設定することもできます。デフォルトでは、 SDK は環境変数または上記で設定したデフォルトキーを用いて `AsyncOpenAI` インスタンスを作成します。これを変更したい場合は、 [set_default_openai_client()][agents.set_default_openai_client] 関数を使用してください。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用しますが、 [set_default_openai_api()][agents.set_default_openai_api] 関数を使用して Chat Completions API に切り替えることができます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。上記セクションの OpenAI API キー (環境変数または設定済みのデフォルトキー) が自動的に使用されます。トレーシング専用の API キーを設定したい場合は、 [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

トレーシングを完全に無効化したい場合は、 [`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用できます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

SDK にはハンドラーが設定されていない 2 つの Python ロガーが用意されています。デフォルトでは、 warning と error は `stdout` に出力されますが、それ以外のログは抑制されます。

詳細なログを有効にするには、 [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることも可能です。詳しくは [Python logging guide](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

ログの中には機密データ (例: ユーザーデータ) を含むものがあります。これらのデータを記録しないようにする場合は、次の環境変数を設定してください。

LLM の入力および出力のロギングを無効化する:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力のロギングを無効化する:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```