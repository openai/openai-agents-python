---
search:
  exclude: true
---
# SDK の設定

## API キーとクライアント

デフォルトでは、SDK はインポートされるとすぐに、LLM リクエストとトレーシングのために `OPENAI_API_KEY` 環境変数を探します。アプリが起動する前にその環境変数を設定できない場合は、 [set_default_openai_key()][agents.set_default_openai_key] 関数でキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

あるいは、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数または上記で設定したデフォルトキーから API キーを用いて `AsyncOpenAI` インスタンスを作成します。これを変更するには、 [set_default_openai_client()][agents.set_default_openai_client] 関数を使用します。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは、OpenAI Responses API を使用します。 [set_default_openai_api()][agents.set_default_openai_api] 関数を使って、Chat Completions API を使用するように上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上記の OpenAI API キー（つまり、環境変数または設定したデフォルトキー）を使用します。トレーシングに使用する API キーを個別に設定するには、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用します。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用して、トレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグロギング

SDK には、ハンドラーが設定されていない 2 つの Python ロガーがあります。デフォルトでは、これにより警告とエラーは `stdout` に送られますが、それ以外のログは抑制されます。

詳細なログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

あるいは、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズできます。詳細は [Python logging guide](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

### ログ内の機微なデータ

一部のログには機微なデータ（たとえば、ユーザー データ）が含まれる場合があります。このデータの記録を無効化したい場合は、次の環境変数を設定してください。

LLM 入出力のロギングを無効化するには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツール入出力のロギングを無効化するには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```