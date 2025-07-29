---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、 SDK はインポートされるとすぐに、 LLM リクエストとトレーシングのために `OPENAI_API_KEY` 環境変数を探します。アプリ起動前にその環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数でキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

別の方法として、使用する OpenAI クライアントを設定することもできます。デフォルトでは、 SDK は環境変数にある API キー、または上記で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを生成します。[set_default_openai_client()][agents.set_default_openai_client] 関数を使うことで、これを変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは、 OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API を利用するよう上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは、上記の OpenAI API キー（つまり環境変数またはあなたが設定したデフォルトキー）を使用します。[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使って、トレーシング専用の API キーを設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

また、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用してトレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

 SDK には、ハンドラが設定されていない Python のロガーが 2 つあります。デフォルトでは、警告とエラーは `stdout` に送られますが、それ以外のログは抑制されます。

詳細ログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

あるいは、ハンドラ、フィルタ、フォーマッタなどを追加してログをカスタマイズすることもできます。詳しくは [Python logging guide](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

一部のログには機密データ（例: ユーザー データ）が含まれる場合があります。これらのデータが記録されないようにするには、次の環境変数を設定してください。

LLM の入力と出力をログに残さないようにするには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力と出力をログに残さないようにするには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```