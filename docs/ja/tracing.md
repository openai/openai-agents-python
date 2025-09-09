---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェントの実行中に発生するイベントの包括的な記録（ LLM の生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントまで）を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使うと、開発中や本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングは既定で有効です。トレーシングを無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. 1 回の実行のみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の APIs を使用し Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース (Traces)** は「ワークフロー」の単一のエンドツーエンドの処理を表します。スパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 省略可能なグループ ID。同じ会話からの複数のトレースを関連付けます。たとえばチャットスレッド ID などが使えます。
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースの任意のメタデータ。
-   **スパン (Spans)** は開始時刻と終了時刻を持つ処理を表します。スパンには次が含まれます:
    -   `started_at` と `ended_at` のタイムスタンプ
    -   所属するトレースを示す `trace_id`
    -   このスパンの親スパン（ある場合）を指す `parent_id`
    -   スパンに関する情報である `span_data`。たとえば、`AgentSpanData` はエージェントに関する情報を、`GenerationSpanData` は LLM 生成に関する情報を含みます。

## 既定のトレーシング

既定では、 SDK は次をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体は `trace()` でラップされます
-   エージェントが実行されるたびに `agent_span()` でラップされます
-   LLM の生成は `generation_span()` でラップされます
-   関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（音声認識）は `transcription_span()` でラップされます
-   音声出力（音声合成）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の子になる場合があります

既定では、トレース名は "Agent workflow" です。`trace` を使う場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレース プロセッサー](#custom-tracing-processors) をセットアップして、他の宛先へトレースを送信できます（置き換え、またはセカンダリ宛先として）。

## より高レベルのトレース

`run()` への複数回の呼び出しを 1 つのトレースにまとめたい場合があります。これには、コード全体を `trace()` でラップします。

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

1. `with trace()` 内に 2 回の `Runner.run` 呼び出しを入れているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数でトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. 推奨: トレースをコンテキストマネージャとして使用します（例: `with trace(...) as my_trace`）。これにより適切なタイミングで自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて追跡されます。これは自動的に並行処理で機能することを意味します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドでスパンを作成できます。一般には、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

スパンは自動的に現在のトレースの一部になり、最も近い現在のスパンの下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。

## 機微なデータ

一部のスパンは機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらに機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によって、そのデータの取得を無効化できます。

同様に、音声スパンは既定で入力および出力音声の base64 エンコードの PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して、この音声データの取得を無効化できます。

## カスタムトレーシング プロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです:

-   初期化時に、トレース作成を担うグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成され、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。エクスポーターはスパンとトレースを OpenAI バックエンドへバッチでエクスポートします。

既定のセットアップをカスタマイズして、代替または追加のバックエンドにトレースを送る、あるいはエクスポーターの動作を変更するには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが用意でき次第それらを受け取る「追加の」トレースプロセッサーを追加できます。これにより、 OpenAI のバックエンドへ送信するのに加えて独自の処理も実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、既定のプロセッサーを独自のトレースプロセッサーで「置き換え」られます。OpenAI バックエンドへトレースを送信するには、その役割を果たす `TracingProcessor` を含める必要があります。

## 非 OpenAI モデルでのトレーシング

OpenAI の API キーを非 OpenAI モデルで使用して、トレーシングを無効化することなく OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。

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

## メモ
- 無料のトレースは Openai Traces ダッシュボードで閲覧できます。

## 外部トレーシング プロセッサー一覧

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