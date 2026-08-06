---
search:
  exclude: true
---
# 設定

このページでは、デフォルトの OpenAI キーやクライアント、デフォルトの OpenAI API 形式、トレーシングのエクスポート設定、ログ動作など、通常はアプリケーションの起動時に一度だけ設定する SDK 全体のデフォルトについて説明します。

これらのデフォルトはサンドボックスベースのワークフローにも適用されますが、サンドボックスのワークスペース、サンドボックスクライアント、セッションの再利用は個別に設定します。

代わりに特定のエージェントや実行を設定する必要がある場合は、以下を参照してください。

-   標準的な `Agent` の instructions、tools、出力型、ハンドオフ、ガードレールについては、[エージェント](agents.md)を参照してください。
-   `RunConfig`、セッション、会話状態のオプションについては、[エージェントの実行](running_agents.md)を参照してください。
-   `SandboxRunConfig`、マニフェスト、機能、サンドボックスクライアント固有のワークスペース設定については、[サンドボックスエージェント](sandbox/guide.md)を参照してください。
-   モデルの選択とプロバイダーの設定については、[モデル](models/index.md)を参照してください。
-   実行ごとのトレーシングメタデータとカスタムトレースプロセッサーについては、[トレーシング](tracing.md)を参照してください。

## 設定オブジェクトと辞書

SDK が管理する設定パラメーターは通常、型付き設定オブジェクト、または同じフィールドを含む辞書のいずれかを受け入れます。これは、型アノテーションに辞書が含まれる、エージェント、実行、モデル、セッション、サンドボックス、音声の各設定境界に適用されます。ネストされた SDK 管理の設定でも辞書を使用できます。

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings={
        "reasoning": {"effort": "high"},
        "verbosity": "low",
    },
)
```

SDK はこれらの辞書を対応する設定オブジェクトに正規化します。SDK が管理する dataclass 設定に不明なフィールドがあると `TypeError` が発生するため、オプション名の入力ミスを早期に検出できます。特定の設定境界が辞書を受け入れるかどうかを確認するには、パラメーターの型アノテーションまたは API リファレンスを参照してください。

## API キーとクライアント

デフォルトでは、SDK は LLM リクエストとトレーシングに `OPENAI_API_KEY` 環境変数を使用します。キーは、SDK が初めて OpenAI クライアントを作成するときに解決されるため（遅延初期化）、最初のモデル呼び出しより前に環境変数を設定してください。アプリケーションの起動前にその環境変数を設定できない場合は、[set_default_openai_key()][agents.set_default_openai_key] 関数を使用してキーを設定できます。

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

また、使用する OpenAI クライアントを設定することもできます。デフォルトでは、SDK は環境変数の API キー、または上記で設定したデフォルトキーを使用して `AsyncOpenAI` インスタンスを作成します。[set_default_openai_client()][agents.set_default_openai_client] 関数を使用すると、これを変更できます。

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

環境変数によるエンドポイント設定を使用する場合、デフォルトの OpenAI プロバイダーは `OPENAI_BASE_URL` も読み取ります。Responses の WebSocket トランスポートを有効にすると、WebSocket の `/responses` エンドポイント用に `OPENAI_WEBSOCKET_BASE_URL` も読み取ります。

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

最後に、使用する OpenAI API をカスタマイズすることもできます。デフォルトでは、OpenAI Responses API を使用します。[set_default_openai_api()][agents.set_default_openai_api] 関数を使用すると、これを上書きして Chat Completions API を使用できます。

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI プロバイダーのデフォルト

OpenAI ベースのプロバイダーも、モデル名を解決するときに SDK 全体のデフォルトを読み取ります。OpenAI Responses モデルでデフォルトで WebSocket トランスポートを使用するには、[`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport] を使用します。

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーによって解決される OpenAI Responses モデルに影響します。プロバイダーレベルの設定、接続の再利用、キープアライブオプション、カスタム WebSocket エンドポイントについては、[Responses WebSocket トランスポート](models/index.md#responses-websocket-transport)を参照してください。

OpenAI の設定でプロバイダーレベルのエージェント登録メタデータが必要な場合は、起動時にデフォルトのハーネス ID を一度設定します。

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

完全な登録オブジェクトを渡すこともできます。

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

SDK のデフォルトが設定されていない場合、OpenAI ベースのプロバイダーは `OPENAI_AGENT_HARNESS_ID` 環境変数にフォールバックします。ハーネス ID が設定されている場合、そのキーが `RunConfig.trace_metadata` にすでに存在しない限り、SDK はトレースメタデータに `agent_harness_id` として追加します。

## トレーシング

トレーシングはデフォルトで有効です。デフォルトでは、上記のセクションにあるモデルリクエストと同じ OpenAI API キー、つまり環境変数または設定したデフォルトキーを使用します。[`set_tracing_export_api_key`][agents.set_tracing_export_api_key] 関数を使用すると、トレーシングに使用する API キーを個別に設定できます。

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

モデルの通信ではあるキーまたはクライアントを使用し、トレーシングでは別の OpenAI キーを使用する必要がある場合は、デフォルトのキーまたはクライアントを設定するときに `use_for_tracing=False` を渡してから、トレーシングを個別に設定します。カスタムクライアントを使用していない場合は、[`set_default_openai_key()`][agents.set_default_openai_key] でも同じ方法を使用できます。

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key")
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

デフォルトのエクスポーターを使用する際に、トレースを特定の組織またはプロジェクトに関連付ける必要がある場合は、アプリケーションの起動前に以下の環境変数を設定します。

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

グローバルエクスポーターを変更せずに、実行ごとにトレーシング用の API キーを設定することもできます。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

[`set_tracing_disabled()`][agents.set_tracing_disabled] 関数を使用して、トレーシングを完全に無効にすることもできます。

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

トレーシングを有効なままにしながら、機密情報を含む可能性がある入出力をトレースペイロードから除外する場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を `False` に設定します。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

アプリケーションの起動前に以下の環境変数を設定すると、コードを使用せずにデフォルトを変更することもできます。

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

トレーシングのすべての制御方法については、[トレーシングガイド](tracing.md)を参照してください。

## デバッグログ

SDK は 2 つの Python ロガー（`openai.agents` と `openai.agents.tracing`）を定義しますが、デフォルトではハンドラーを追加しません。ログは、アプリケーションの Python ロギング設定に従います。

詳細なログを有効にするには、[`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] 関数を使用します。

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

また、ハンドラー、フィルター、フォーマッターなどを追加して、ログをカスタマイズすることもできます。詳しくは、[Python ロギングガイド](https://docs.python.org/3/howto/logging.html)を参照してください。

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

### ログと診断情報内の機密データ

一部のログや診断例外には、機密データ（モデルまたはツールの入出力など）が含まれる場合があります。

デフォルトでは、SDK は LLM の入出力やツールの入出力を **ログに記録しません**。これらの保護は、以下によって制御されます。

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

デバッグのために一時的にこのデータを含める必要がある場合は、アプリケーションの起動前にいずれかの変数を `0`（または `false`）に設定します。

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```

これらのフラグは、影響を受ける失敗に、ペイロードを含む診断の詳細を保持するかどうかも制御します。たとえば、ツールデータの編集が有効な場合、関数ツールへの無効な引数によって、元の検証エラーを例外チェーンに含まない汎用的な `ModelBehaviorError` が発生します。いずれかの変数を `0` に設定すると、未加工のモデルまたはツールデータが、ログ、例外メッセージ、例外チェーン、その他の診断コンテキストに露出する可能性があるため、管理された開発環境でのみ有効にしてください。