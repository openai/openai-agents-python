---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベントを網羅的に記録します。具体的には、 LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらには発生するカスタムイベントまで含みます。[Traces ダッシュボード](https://platform.openai.com/traces)を使うと、開発時および本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. 単一の実行でトレーシングを無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の API を利用し、 Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース (Traces)** は「ワークフロー」の単一のエンドツーエンド操作を表します。スパンで構成されます。トレースには次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 省略可能なグループ ID。同一会話の複数トレースを関連付けるために使用します。たとえばチャットスレッドの ID などです。
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースの任意のメタデータ。
-   **スパン (Spans)** は開始時刻と終了時刻を持つ操作を表します。スパンには次の情報があります。
    -   `started_at` および `ended_at` タイムスタンプ
    -   所属するトレースを示す `trace_id`
    -   親スパン (ある場合) を指す `parent_id`
    -   スパンに関する情報である `span_data`。たとえば、`AgentSpanData` はエージェントに関する情報、`GenerationSpanData` は LLM 生成に関する情報などです。

## デフォルトのトレーシング

デフォルトでは、 SDK は次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます
-   エージェントが実行されるたびに `agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力 (音声認識) は `transcription_span()` でラップされます
-   音声出力 (音声合成) は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の下に配置される場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors)を設定して、他の宛先へトレースを送信できます (置き換えまたは追加の宛先として)。

## 上位レベルのトレース

複数回の `run()` 呼び出しを単一のトレースに含めたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。次の 2 つの方法があります。

1. 推奨: トレースをコンテキストマネージャーとして使用します。例: `with trace(...) as my_trace`。これにより適切なタイミングでトレースが自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて追跡されます。これにより自動的に並行処理に対応します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般に、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

スパンは自動的に現在のトレースの一部となり、 Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡される最も近い現在のスパンの下にネストされます。

## 機微なデータ

一部のスパンは機微なデータを含む可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機微なデータが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそのデータのキャプチャを無効化できます。

同様に、音声スパンにはデフォルトで、入力および出力音声の base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで、この音声データのキャプチャを無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時に、トレースの作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。これにより、スパンとトレースが OpenAI のバックエンドにバッチでエクスポートされます。

このデフォルト設定をカスタマイズして、別のバックエンドへ送信したり、追加のバックエンドへ送信したり、エクスポーターの動作を変更するには次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが準備でき次第受け取る「追加の」トレースプロセッサーを追加できます。これにより、 OpenAI のバックエンドへの送信に加えて独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに「置き換え」られます。これにより、 OpenAI のバックエンドにトレースが送信されなくなります (その送信を行う `TracingProcessor` を含めない限り)。

## Non-OpenAI Models でのトレーシング

OpenAI の API キーを Non-OpenAI Models と一緒に使用して、トレーシングを無効化することなく OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。

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

## 注意
- 無料のトレースは OpenAI Traces ダッシュボードで確認できます。

## 外部トレーシングプロセッサーの一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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