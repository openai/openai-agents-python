---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらに発生したカスタムイベント）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する一般的な方法は 3 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使ってコード内でトレーシングをグローバルに無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行に対してトレーシングを無効化できます

***OpenAI の API を使用し、 Zero Data Retention ( ZDR ) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース**は「ワークフロー」の単一のエンドツーエンド操作を表します。トレースはスパンで構成されます。トレースには次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。たとえば「Code generation」や「Customer service」です。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: オプションのグループ ID で、同じ会話からの複数のトレースを関連付けるために使用します。たとえばチャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースのオプションのメタデータです。
-   **スパン**は開始時刻と終了時刻を持つ操作を表します。スパンには次があります。
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`。所属するトレースを表します
    -   `parent_id`。このスパンの親スパン（存在する場合）を指します
    -   `span_data`。スパンに関する情報です。たとえば `AgentSpanData` にはエージェントの情報が含まれ、`GenerationSpanData` には LLM 生成の情報が含まれます。

## デフォルトのトレーシング

デフォルトでは、 SDK は次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体は `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（ speech-to-text ）は `transcription_span()` でラップされます
-   音声出力（ text-to-speech ）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の配下になる場合があります

デフォルトでは、トレース名は「Agent workflow」です。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先へ送ることができます（置き換えまたは副次的な送信先として）。

## 高レベルのトレース

場合によっては、複数回の `run()` 呼び出しを単一のトレースの一部にしたいことがあります。これはコード全体を `trace()` でラップすることで実現できます。

```python
from agents import Agent, Runner, trace

async def main():
    agent = Agent(name="Joke generator", instructions="Tell funny jokes.")

    with trace("Joke workflow"): # (1)!
        first_result = await Runner.run(agent, "Tell me a joke")
        second_result = await Runner.run(agent, f"Rate this joke: {first_result.final_output}")
        print(f"Joke: {first_result.final_output}")
        print(f"Rating: {second_result.final_output}")
```

1. `Runner.run` への 2 回の呼び出しが `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります。

1. **推奨**: `with trace(...) as my_trace` のように、トレースをコンテキストマネージャーとして使用します。これにより、適切なタイミングでトレースが自動的に開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これは並行処理でも自動的に機能することを意味します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般に、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数が利用できます。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの配下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。

## 機密データ

特定のスパンは、機密性の高い可能性があるデータを取得する場合があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそのデータの取得を無効化できます。

同様に、音声スパンにはデフォルトで入力および出力音声の base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで、この音声データの取得を無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。コードを変更せずにデフォルトを設定するには、アプリ実行前に環境変数 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` を `true/1` または `false/0` に設定します。

## カスタムトレースプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時に、トレース作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。`BackendSpanExporter` はスパンとトレースをバッチで OpenAI バックエンドにエクスポートします。

このデフォルト設定をカスタマイズして、代替または追加のバックエンドにトレースを送信したり、エクスポーターの挙動を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使うと、準備できたトレースとスパンを受け取る**追加の**トレースプロセッサーを追加できます。これにより、OpenAI バックエンドへの送信に加えて独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使うと、デフォルトプロセッサーを独自のトレースプロセッサーで**置き換え**できます。これは、そうした処理を行う `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドに送信されないことを意味します。


## 非 OpenAI モデルでのトレーシング

OpenAI API キーを非 OpenAI モデルとともに使用して、トレーシングを無効化せずに OpenAI Traces ダッシュボードで無料トレーシングを有効化できます。

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
from agents.extensions.models.litellm_model import LitellmModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = LitellmModel(
    model="your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

単一の実行に対してのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに `RunConfig` 経由で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 追加メモ
- Openai Traces ダッシュボードで無料トレースを表示します。


## エコシステム統合

以下のコミュニティおよびベンダー統合は、OpenAI Agents SDK のトレーシング機能をサポートしています。

### 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Keywords AI](https://docs.keywordsai.co/integration/development-frameworks/openai-agent)
-   [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-   [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)
-   [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/llm-analytics/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents)