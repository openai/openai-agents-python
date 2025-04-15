# SDK の設定

## API キーとクライアント

デフォルトでは、SDK はインポート時に LLM リクエストやトレーシングのために `OPENAI_API_KEY` 環境変数を探します。アプリの起動前にこの環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使ってキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することも可能です。デフォルトでは、SDK は環境変数または上記で設定したデフォルトキーを使って `AsyncOpenAI` インスタンスを作成します。これを変更したい場合は、[set_default_openai_client()][agents.set_default_openai_client] 関数を利用してください。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用していますが、[set_default_openai_api()][agents.set_default_openai_api] 関数を使って Chat Completions API を利用するように上書きできます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは、上記のセクションで説明した OpenAI API キー（環境変数または設定したデフォルトキー）を使用します。トレーシング専用の API キーを設定したい場合は、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を利用してください。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

また、[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使ってトレーシングを完全に無効化することもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

## デバッグログ

SDK には、ハンドラーが設定されていない 2 つの Python ロガーがあります。デフォルトでは、警告やエラーは `stdout` に送信されますが、それ以外のログは抑制されます。

詳細なログ出力を有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用してください。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラーやフィルター、フォーマッターなどを追加してログをカスタマイズすることも可能です。詳細は [Python ロギングガイド](https://docs.python.org/3/howto/logging.html) をご覧ください。

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

一部のログには機微なデータ（たとえば ユーザー データ）が含まれる場合があります。これらのデータのログ出力を無効にしたい場合は、以下の環境変数を設定してください。

LLM の入力および出力のログ出力を無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力のログ出力を無効にするには:

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```