# SDK の設定

## API キーとクライアント

デフォルトでは、SDK はインポート時に LLM リクエストおよびトレーシング用の環境変数 `OPENAI_API_KEY` を参照します。アプリケーションの起動前にこの環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することも可能です。デフォルトでは、SDK は環境変数または上記で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを作成します。これを変更するには、[set_default_openai_client()][agents.set_default_openai_client] 関数を使用します。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

さらに、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは OpenAI Responses API を使用しますが、[set_default_openai_api()][agents.set_default_openai_api] 関数を使用して Chat Completions API に変更できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## トレーシング

トレーシングはデフォルトで有効になっています。デフォルトでは、前述のセクションで設定した OpenAI API キー（環境変数またはデフォルトキー）を使用します。トレーシング専用の API キーを設定するには、[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用します。

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

SDK はデフォルトでハンドラーが設定されていない 2 つの Python ロガーを持っています。デフォルトでは、警告およびエラーが `stdout` に送信され、それ以外のログは抑制されます。

詳細なログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加してログをカスタマイズすることも可能です。詳細については、[Python logging ガイド](https://docs.python.org/3/howto/logging.html) を参照してください。

```python
import logging

logger = logging.getLogger("openai.agents") # トレーシング用ロガーの場合は openai.agents.tracing

# すべてのログを表示する場合
logger.setLevel(logging.DEBUG)
# INFO 以上を表示する場合
logger.setLevel(logging.INFO)
# WARNING 以上を表示する場合
logger.setLevel(logging.WARNING)
# その他、必要に応じて設定

# 必要に応じてカスタマイズ可能ですが、デフォルトでは `stderr` に出力されます
logger.addHandler(logging.StreamHandler())
```

### ログ内の機密データ

一部のログには機密データ（ユーザーデータなど）が含まれる場合があります。このデータのログ記録を無効化したい場合は、以下の環境変数を設定してください。

LLM の入力および出力のログ記録を無効化する場合：

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
```

ツールの入力および出力のログ記録を無効化する場合：

```bash
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```