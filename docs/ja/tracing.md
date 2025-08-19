---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェント実行中に発生するイベントの包括的な記録を収集します。LLM の生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまで対象です。[Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発中および本番環境でワークフローのデバッグ、可視化、監視ができます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 1 回の実行についてのみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

 ***OpenAI の APIs を使用し、Zero Data Retention (ZDR) ポリシー下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は「ワークフロー」の単一のエンドツーエンド操作を表します。スパンで構成されます。トレースには以下のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意 ID。渡さなければ自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同一会話からの複数トレースを紐づける任意のグループ ID。例: チャットスレッド ID の利用
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの任意メタデータ
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには以下があります。
    -   `started_at` および `ended_at` タイムスタンプ
    -   所属するトレースを表す `trace_id`
    -   親スパンを指す `parent_id`（ある場合）
    -   スパンに関する情報である `span_data`。たとえば、`AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成に関する情報など

## 既定のトレーシング

デフォルトで、SDK は次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` にラップされます。
-   エージェントが実行されるたびに `agent_span()` にラップされます
-   LLM の生成は `generation_span()` にラップされます
-   関数ツールの呼び出しはそれぞれ `function_span()` にラップされます
-   ガードレールは `guardrail_span()` にラップされます
-   ハンドオフは `handoff_span()` にラップされます
-   音声入力（音声認識）は `transcription_span()` にラップされます
-   音声出力（音声合成）は `speech_span()` にラップされます
-   関連する音声スパンは `speech_group_span()` の配下に配置される場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成することもできます。

さらに、[カスタム トレース プロセッサー](#custom-tracing-processors) を設定して、トレースを別の送信先（置き換えまたは副次的送信先）に送信できます。

## 上位レベルのトレース

`run()` の複数回呼び出しを 1 つのトレースにまとめたい場合があります。コード全体を `trace()` でラップすれば可能です。

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

1. `with trace()` で 2 回の `Runner.run` 呼び出しをラップしているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数でトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります。

1. 【推奨】トレースをコンテキストマネージャとして使用します（例: `with trace(...) as my_trace`）。これにより、適切なタイミングで自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて管理されます。これにより、自動的に並行実行で動作します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドでスパンを作成できます。一般的に、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために、[`custom_span()`][agents.tracing.custom_span] 関数を使用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡される、最も近い現在のスパンの配下にネストされます。

## 機微なデータ

特定のスパンは機微なデータを記録する可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。機微なデータを含む場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの捕捉を無効化できます。

同様に、音声スパンはデフォルトで入出力音声の base64 エンコード PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成して、この音声データの捕捉を無効化できます。

## カスタム トレーシング プロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです。

-   初期化時に、トレースを作成する役割を持つグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` を、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信する [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成します。`BackendSpanExporter` はスパンとトレースを OpenAI バックエンドへバッチエクスポートします。

この既定の構成をカスタマイズして、別のバックエンドへの送信や追加のバックエンドへの送信、またはエクスポーターの動作を変更するには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースとスパンが準備でき次第受け取る「追加の」トレース プロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、既定のプロセッサーを独自のトレース プロセッサーに「置き換え」られます。これを行うと、OpenAI バックエンドにトレースは送信されません（送信する `TracingProcessor` を含めない限り）。

## 非 OpenAI モデルでのトレーシング

OpenAI の API キーを非 OpenAI モデルで使用すると、トレーシングを無効化することなく、OpenAI Traces ダッシュボードで無料のトレーシングを有効化できます。

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
- Openai Traces ダッシュボードで無料のトレースを表示できます。

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