---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベントを包括的に記録します。LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで対象です。[Traces ダッシュボード](https://platform.openai.com/traces)を使って、開発中や本番運用中のワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 1 回の実行に対してのみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の API を Zero Data Retention (ZDR) ポリシーの下で利用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は「ワークフロー」の単一のエンドツーエンド操作を表します。複数のスパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: これは論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: オプションのグループ ID。同一の会話からの複数のトレースをリンクするために使用します。例えばチャットスレッド ID など。
    -   `disabled`: True の場合、そのトレースは記録されません。
    -   `metadata`: トレースのためのオプションのメタデータ。
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次が含まれます:
    -   `started_at` と `ended_at` のタイムスタンプ
    -   所属するトレースを表す `trace_id`
    -   親スパンを指す `parent_id`（ある場合）
    -   スパンに関する情報である `span_data`。例えば、`AgentSpanData` にはエージェントに関する情報、`GenerationSpanData` には LLM 生成に関する情報などが含まれます。

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします:

-   全体の `Runner.{run, run_sync, run_streamed}()` は `trace()` でラップされます
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（音声→テキスト）は `transcription_span()` でラップされます
-   音声出力（テキスト→音声）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の下にネストされる場合があります

デフォルトのトレース名は "Agent workflow" です。`trace` を使う場合にこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、他の送信先へトレースを送ることができます（置き換え、またはセカンダリ送信先として）。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。その場合、全体のコードを `trace()` でラップします。

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

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。次の 2 つの方法があります:

1. 推奨: トレースをコンテキストマネージャーとして使用します（例: `with trace(...) as my_trace`）。これにより適切なタイミングで自動的に開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これにより自動的に並行実行に対応します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。一般的にはスパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される直近の現在スパンの下にネストされます。

## 機微情報

特定のスパンは機微情報を含む可能性があります。

`generation_span()` は LLM 生成の入出力を保存し、`function_span()` は関数呼び出しの入出力を保存します。これらに機微情報が含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそれらのデータの収集を無効化できます。

同様に、音声スパンにはデフォルトで入力・出力音声の base64 エンコードされた PCM データが含まれます。この音声データの収集は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、これがトレースの作成を担当します。
-   `TraceProvider` には [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、これはトレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。エクスポーターはスパンとトレースを OpenAI バックエンドにバッチでエクスポートします。

このデフォルト構成をカスタマイズして、別のバックエンドや追加のバックエンドへトレースを送信したり、エクスポーターの動作を変更したりするには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが準備できた時点で受け取る「追加の」トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに「置き換え」られます。これは、OpenAI バックエンドに送信する `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドに送られないことを意味します。


## OpenAI 以外のモデルでのトレーシング

OpenAI の API キーを非 OpenAI モデルで使用すると、トレーシングを無効化せずに OpenAI の Traces ダッシュボードで無料のトレーシングを有効にできます。

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

## 備考
- 無料のトレースは OpenAI Traces ダッシュボードで確認できます。

## 外部トレーシングプロセッサー一覧

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