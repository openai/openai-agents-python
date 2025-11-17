---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェント実行中に発生するイベントの包括的な記録を収集します。LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで含まれます。[Traces ダッシュボード](https://platform.openai.com/traces)を使用して、開発時および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化できます
    2. 1 回の実行に対しては、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して無効化できます

***OpenAI の API を用いた Zero Data Retention (ZDR) ポリシー下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-  **トレース** は「ワークフロー」の単一のエンドツーエンド処理を表します。スパンで構成されます。トレースには次のプロパティがあります。
    -  `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -  `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -  `group_id`: 任意のグループ ID。同じ会話からの複数のトレースをリンクするために使用します。たとえばチャットスレッド ID を使用できます。
    -  `disabled`: True の場合、トレースは記録されません。
    -  `metadata`: トレースの任意のメタデータ。
-  **スパン** は開始時刻と終了時刻を持つ処理を表します。スパンには次があります。
    -  `started_at` と `ended_at` のタイムスタンプ
    -  所属するトレースを表す `trace_id`
    -  親スパン (ある場合) を指す `parent_id`
    -  スパンに関する情報である `span_data`。たとえば、`AgentSpanData` にはエージェントに関する情報、`GenerationSpanData` には LLM 生成に関する情報などが含まれます。

## 既定のトレーシング

デフォルトでは、SDK は次の内容をトレースします。

-  全体の `Runner.{run, run_sync, run_streamed}()` は `trace()` でラップされます。
-  エージェントが実行されるたびに `agent_span()` でラップされます
-  LLM 生成は `generation_span()` でラップされます
-  関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
-  ガードレールは `guardrail_span()` でラップされます
-  ハンドオフは `handoff_span()` でラップされます
-  音声入力 (音声からテキスト) は `transcription_span()` でラップされます
-  音声出力 (テキストから音声) は `speech_span()` でラップされます
-  関連する音声スパンは `speech_group_span()` の下に親付けされる場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定でき、または [`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを別の送信先に送信できます (置き換えとして、またはセカンダリ送信先として)。

## 上位レベルのトレース

場合によっては、複数の `run()` 呼び出しを 1 つのトレースの一部にしたいことがあります。これを実現するには、コード全体を `trace()` でラップします。

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

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。次の 2 つの方法があります。

1. 推奨: トレースをコンテキストマネージャーとして使用します。例: `with trace(...) as my_trace`。これにより、適切なタイミングでトレースが自動的に開始・終了されます。
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。これにより並行処理でも自動的に機能します。トレースを手動で開始・終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

各種の [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般的には、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数が利用可能です。

スパンは自動的に現在のトレースの一部になり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される最も近い現在のスパンの下にネストされます。

## 機微なデータ

一部のスパンは、機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を保存します。これらには機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの取得を無効化できます。

同様に、音声スパンはデフォルトで入力および出力の音声について base64 エンコードされた PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで、この音声データの取得を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです。

-  初期化時に、トレースの作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-  `TraceProvider` を [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成し、スパンとトレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。これがスパンとトレースを OpenAI バックエンドにバッチでエクスポートします。

このデフォルト設定をカスタマイズし、別のバックエンドへの送信や追加のバックエンドへの送信、エクスポーターの動作変更を行うには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが準備でき次第受け取る、追加入力のトレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに置き換えることができます。これにより、OpenAI バックエンドにトレースが送信されなくなります (そのため、送信する `TracingProcessor` を含めない限り送信されません)。

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

## 注意
- 無料のトレースは OpenAI Traces ダッシュボードで確認できます。

## 外部トレーシングプロセッサー一覧

-  [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-  [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-  [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-  [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-  [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-  [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-  [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-  [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-  [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-  [Keywords AI](https://docs.keywordsai.co/integration/development-frameworks/openai-agent)
-  [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-  [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-  [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-  [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-  [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-  [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-  [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-  [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-  [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)
-  [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)