---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、 エージェント 実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなどの包括的な記録を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発時および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. 単一の実行でトレーシングを無効化する場合は、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーの下で利用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は 1 つの "ワークフロー" のエンドツーエンドの操作を表します。トレースはスパンで構成されます。トレースには次のプロパティがあります。  
    -   `workflow_name`: 論理的なワークフローまたはアプリの名前。例: 「Code generation」や「Customer service」  
    -   `trace_id`: トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    -   `group_id`: 省略可能なグループ ID。同じ会話から複数のトレースをリンクする際に使用します。例としてチャットスレッド ID など。  
    -   `disabled`: `True` の場合、このトレースは記録されません。  
    -   `metadata`: トレースのメタデータ (任意)。  
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次が含まれます。  
    -   `started_at` と `ended_at` タイムスタンプ  
    -   `trace_id`: 所属するトレースを示します  
    -   `parent_id`: このスパンの親スパン (存在する場合) を指します  
    -   `span_data`: スパンに関する情報。たとえば `AgentSpanData` は エージェント 情報、`GenerationSpanData` は LLM 生成情報などを含みます。  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます  
-   エージェント が実行されるたびに `agent_span()` でラップされます  
-   LLM 生成は `generation_span()` でラップされます  
-   関数ツール 呼び出しはそれぞれ `function_span()` でラップされます  
-   ガードレール は `guardrail_span()` でラップされます  
-   ハンドオフ は `handoff_span()` でラップされます  
-   音声入力 (音声→テキスト) は `transcription_span()` でラップされます  
-   音声出力 (テキスト→音声) は `speech_span()` でラップされます  
-   関連する音声スパンは `speech_group_span()` の下にネストされる場合があります  

デフォルトでは、トレース名は「Agent workflow」です。`trace` を使用してこの名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先へプッシュできます (置き換えまたは追加送信先)。

## より高レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。その場合、コード全体を `trace()` でラップします。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` に包まれているため、個別に 2 つのトレースを作成するのではなく、1 つのトレースにまとめられます。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要で、次の 2 通りの方法があります。

1. **推奨**: コンテキストマネージャとして trace を使用する (`with trace(...) as my_trace`)。これにより、適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されるため、並行処理でも自動的に機能します。トレースを手動で開始・終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。通常は手動でスパンを作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される最も近い現在のスパンの下にネストされます。

## 機微データ

特定のスパンは機微データを含む場合があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は 関数ツール 呼び出しの入出力を保存します。これらに機微データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの保存を無効化できます。

同様に、音声スパンにはデフォルトで base64 エンコードされた PCM データ (入力および出力) が含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで、この音声データの保存を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースを生成します。  
-   その `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。Exporter はスパンとトレースを OpenAI バックエンドへバッチ送信します。  

このデフォルト設定をカスタマイズし、他のバックエンドへトレースを送信したり、Exporter の動作を変更するには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用して **追加の** トレースプロセッサーを登録し、トレース/スパンを受け取って独自に処理できます (OpenAI バックエンドへの送信に加えて処理を行う場合)。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用してデフォルトのプロセッサーを **置き換え** ます。この場合、OpenAI バックエンドへトレースを送信するには、送信用の `TracingProcessor` を含める必要があります。  

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