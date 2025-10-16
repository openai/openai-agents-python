---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングがあり、エージェントの実行中に発生するイベントの包括的な記録を収集します。たとえば LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントなどです。[トレース ダッシュボード](https://platform.openai.com/traces)を使用すると、開発時や本番環境でワークフローのデバッグ、可視化、監視ができます。

!!!note

    トレーシングはデフォルトで有効です。トレーシングを無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 1 回の実行のみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の API を使用する Zero Data Retention (ZDR) ポリシー下で運用する組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は「ワークフロー」の単一のエンドツーエンド処理を表します。スパンで構成されます。トレースには次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"。
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 省略可能なグループ ID。同一の会話からの複数のトレースをリンクするために使用します。たとえばチャットスレッド ID を使用できます。
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースのための任意のメタデータ。
-   **スパン** は開始時刻と終了時刻を持つ処理を表します。スパンには次の情報があります。
    -   `started_at` および `ended_at` タイムスタンプ。
    -   `trace_id`: 所属するトレースを表します
    -   `parent_id`: このスパンの親スパン (存在する場合) を指します
    -   `span_data`: スパンに関する情報。たとえば、`AgentSpanData` はエージェントに関する情報、`GenerationSpanData` は LLM 生成に関する情報などです。

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします。

-   全体の `Runner.{run, run_sync, run_streamed}()` が `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力 (音声→テキスト) は `transcription_span()` でラップされます
-   音声出力 (テキスト→音声) は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の下に親子関係で配置される場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成することもできます。

さらに、[カスタム トレース プロセッサー](#custom-tracing-processors) を設定して、トレースを別の宛先に送信できます (置き換え、または第 2 の宛先として)。

## 高レベルのトレース

`run()` を複数回呼び出す場合、それらを 1 つのトレースにまとめたいことがあります。その場合は、コード全体を `trace()` でラップします。

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

1. 【推奨】トレースをコンテキストマネージャとして使用します。すなわち `with trace(...) as my_trace`。これにより適切なタイミングでトレースが自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて追跡されます。これは自動的に並行処理で機能することを意味します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般には、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数を利用できます。

スパンは自動的に現在のトレースの一部になり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される最も近い現在のスパンの下にネストされます。

## 機微なデータ

一部のスパンは機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそのデータの取得を無効化できます。

同様に、音声スパンにはデフォルトで入力および出力音声の base64 エンコード PCM データが含まれます。この音声データの取得は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで無効化できます。

## カスタム トレーシング プロセッサー

トレーシングのハイレベルなアーキテクチャは次のとおりです。

-   初期化時に、トレースの作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を構成し、これがトレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。エクスポーターはスパンとトレースを OpenAI のバックエンドへバッチでエクスポートします。

このデフォルト構成をカスタマイズし、別のバックエンドへの送信や追加のバックエンドへの送信、エクスポーターの動作変更を行うには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用すると、トレースやスパンが準備でき次第受け取る、**追加の** トレース プロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用すると、デフォルトのプロセッサーを独自のトレース プロセッサーに**置き換え**られます。つまり、OpenAI のバックエンドに送信する `TracingProcessor` を含めない限り、トレースは OpenAI のバックエンドに送信されません。

## OpenAI 以外のモデルでのトレーシング

OpenAI の API キーを OpenAI 以外のモデルと併用して、トレーシングを無効化することなく OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。

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
- OpenAI Traces ダッシュボードで無料のトレースを表示できます。

## 外部トレーシング プロセッサー一覧

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