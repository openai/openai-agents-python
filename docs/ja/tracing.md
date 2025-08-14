---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、 エージェント 実行中に発生するイベントの包括的な記録を収集します。LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、そしてカスタムイベントまで記録します。[Traces ダッシュボード](https://platform.openai.com/traces) を使うと、開発時や本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングは既定で有効です。トレーシングを無効化する方法は次の 2 つです。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 1 回の実行に対してのみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース (Traces)** は「ワークフロー」の単一のエンドツーエンドの処理を表します。スパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同じ会話からの複数トレースを関連付けるためのオプションのグループ ID。たとえばチャットスレッド ID を使えます。
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースのオプションのメタデータ。
-   **スパン (Spans)** は開始時刻と終了時刻を持つ処理を表します。スパンには次があります:
    -   `started_at` と `ended_at` のタイムスタンプ
    -   所属するトレースを表す `trace_id`
    -   このスパンの親スパン (あれば) を指す `parent_id`
    -   スパンに関する情報である `span_data`。たとえば、`AgentSpanData` には エージェント に関する情報が、`GenerationSpanData` には LLM 生成に関する情報が含まれます。

## 既定のトレーシング

既定では、SDK は次をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます
-   エージェント が実行されるたびに `agent_span()` でラップされます
-   LLM の生成は `generation_span()` でラップされます
-   関数ツール の呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力 (音声認識) は `transcription_span()` でラップされます
-   音声出力 (音声合成) は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の下に親子付けされる場合があります

既定では、トレース名は "エージェント ワークフロー" です。`trace` を使用する場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成することもできます。

さらに、[カスタム トレーシング プロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先へ送ることができます (置き換えまたは第 2 の送信先として)。

## より高レベルのトレース

複数回の `run()` 呼び出しを単一のトレースの一部にしたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. 推奨: `with trace(...) as my_trace` のように、トレースをコンテキストマネージャーとして使います。これにより、適切なタイミングでトレースが自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これにより、自動的に並行実行でも機能します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。一般的には、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数を利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される、最も近い現在のスパンの下にネストされます。

## センシティブなデータ

特定のスパンは、機微なデータを含む可能性があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を保存します。これらにはセンシティブなデータが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそれらのデータの取得を無効化できます。

同様に、音声スパンには、既定で入力および出力音声の base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで、この音声データの取得を無効化できます。

## カスタム トレーシング プロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです:

-   初期化時に、トレースを作成する役割を持つグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` には [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、これがトレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。エクスポーターはスパンとトレースを OpenAI バックエンドにバッチでエクスポートします。

既定のセットアップをカスタマイズし、別のバックエンドへの送信や追加のバックエンドへの送信、またはエクスポーターの動作を変更するには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが準備でき次第受け取る、追加のトレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて、独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、既定のプロセッサーを独自のトレースプロセッサーに置き換えることができます。つまり、OpenAI バックエンドにトレースを送信する `TracingProcessor` を含めない限り、トレースは OpenAI バックエンドに送信されません。

## 非 OpenAI モデルでのトレーシング

OpenAI の API キーを、非 OpenAI モデルと一緒に使用して、トレーシングを無効化することなく、OpenAI Traces ダッシュボードで無料トレーシングを有効にできます。

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
- 無料トレースは OpenAI Traces ダッシュボードで閲覧できます。

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